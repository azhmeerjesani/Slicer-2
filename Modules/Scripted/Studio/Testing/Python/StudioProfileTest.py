"""Tests of the Slicer Studio user profile (``Base/Python/studio_profile.py``, ``StudioLib/profile_legacy.py``) and of
what keeps user data safe when the application is replaced (the updater's clean-ups).

Plain Python (``python3 StudioProfileTest.py``), also run by the Studio test suite. Only temporary folders are used:
HOME, the profile and the fake installations are all inside a temporary folder; Qt and the application are faked.
"""

import configparser
import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "..", ".."))
for _path in (os.path.join(SOURCE_DIR, "Modules", "Scripted", "Studio"), os.path.join(SOURCE_DIR, "Base", "Python")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import studio_overlay  # noqa: E402
import studio_profile  # noqa: E402
from StudioLib import profile_legacy  # noqa: E402
from StudioLib import updates  # noqa: E402
from StudioLib import updates_commit  # noqa: E402
from StudioLib import updates_install  # noqa: E402

SERVER_REVISION = "35389"


#
# Fakes of Qt and of the application
#


def readIni(path):
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    values = {}
    for section in parser.sections():
        for key, value in parser.items(section):
            name = key.replace("\\", "/") if section == "General" else f"{section}/{key.replace(chr(92), '/')}"
            values[name] = [item.strip() for item in value.split(",")] if ", " in value else value
    return values


def writeIni(path, values):
    groups = {}
    for key, value in values.items():
        group, _separator, rest = key.partition("/")
        if not rest:
            group, rest = "General", key
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        groups.setdefault(group, []).append((rest.replace("/", "\\"), value))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for group, items in groups.items():
            f.write(f"[{group}]\n")
            for key, value in items:
                f.write(f"{key}={value}\n")
            f.write("\n")


class FakeSettings:
    IniFormat = 1

    def __init__(self, path=None, fmt=None, values=None):
        self.path = path
        self.values = dict(values or {})
        self.writes = []
        if path and os.path.exists(path):
            self.values.update(readIni(path))

    def value(self, key, default=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.writes.append(key)
        self.values[key] = value

    def remove(self, key):
        self.writes.append(key)
        for name in [name for name in self.values if name == key or name.startswith(key + "/")]:
            del self.values[name]

    def contains(self, key):
        return key in self.values

    def allKeys(self):
        return list(self.values)

    def sync(self):
        if self.path:
            writeIni(self.path, self.values)


def loadJson(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def readText(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def fakeQt(defaultLocation):
    qt = types.ModuleType("qt")
    qt.QSettings = FakeSettings
    qt.QStandardPaths = types.SimpleNamespace(AppLocalDataLocation=0, writableLocation=lambda location: defaultLocation)
    return qt


def fakeApp(home, revision, revisionFile, userSettings, revisionSettings, os_="linux", testing=False, name="Slicer"):
    environment = {}
    return types.SimpleNamespace(
        applicationName=name, slicerHome=home, isInstalled=True, revision=str(revision), os=os_, arch="amd64",
        applicationVersion="5.13.0", slicerRevisionUserSettingsFilePath=revisionFile, testingEnabled=testing,
        commandOptions=lambda: types.SimpleNamespace(testingEnabled=testing), userSettings=lambda: userSettings,
        revisionUserSettings=lambda: revisionSettings, defaultSettings=lambda: None,
        setEnvironmentVariable=lambda key, value: environment.__setitem__(key, value), environment=environment)


def s4ext(name, revision=SERVER_REVISION, os_="linux"):
    return f"extensionname {name}\nslicer_revision {revision}\nos {os_}\narch amd64\ninstalled true\n"


def makeExtension(folder, name, revision=SERVER_REVISION, os_="linux", payload=b"library"):
    os.makedirs(os.path.join(folder, name, "lib", "Slicer-5.13", "qt-scripted-modules"), exist_ok=True)
    with open(os.path.join(folder, name, "lib", "Slicer-5.13", "qt-scripted-modules", f"{name}.py"), "wb") as f:
        f.write(payload)
    with open(os.path.join(folder, f"{name}.s4ext"), "w", encoding="utf-8") as f:
        f.write(s4ext(name, revision, os_))


def makeStudioHome(home, commit="a" * 40):
    os.makedirs(os.path.join(home, "bin", "Python"), exist_ok=True)
    with open(os.path.join(home, "bin", "Python", "studio_build_info.json"), "w", encoding="utf-8") as f:
        json.dump({"format": 1, "commit": commit}, f)


def _lockWorker(profileRoot, source, barrier, results, hold):
    """Run the locked part of a start-up: copy ``source`` into the profile once."""
    barrier.wait()
    with studio_profile.ProfileLock(profileRoot) as lock:
        if not lock.acquired:
            results.put("skipped")
            return
        destination = os.path.join(profileRoot, "copied")
        if os.path.exists(destination):
            results.put("already")
            return
        problem = studio_profile.copyTreeVerified(source, destination)
        time.sleep(hold)
        results.put("copied" if problem is None else problem)


def _holdLock(profileRoot, ready):
    lock = studio_profile.ProfileLock(profileRoot)
    lock.__enter__()
    ready.set()
    time.sleep(60)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tempDir = tempfile.mkdtemp(prefix="StudioProfileTest-")
        self.addCleanup(shutil.rmtree, self.tempDir, True)
        self.home = os.path.join(self.tempDir, "home")
        os.makedirs(self.home)
        studio_profile.setRoot(None)
        self.addCleanup(studio_profile.setRoot, None)
        studio_profile._status.clear()

    def path(self, *parts):
        return os.path.join(self.tempDir, *parts)


class RootTest(_Base):
    def test_Precedence(self):
        default = self.path("default")
        env = {studio_profile.ENVIRONMENT_VARIABLE: self.path("fromEnv")}
        self.assertEqual(studio_profile.resolveRoot(env, self.home, "linux", self.path("fromSetting"), default=default)[:2], (self.path("fromEnv"), "environment"))
        self.assertEqual(studio_profile.resolveRoot({}, self.home, "linux", self.path("fromSetting"), default=default)[:2], (self.path("fromSetting"), "setting"))
        self.assertEqual(studio_profile.resolveRoot({}, self.home, "linux", None, default=default)[:2], (default, "default"))
        self.assertEqual(studio_profile.resolveRoot({}, self.home, "linux", "~/p", default=default)[0], os.path.join(self.home, "p"))
        for value in ("", "   ", "relative/folder", "~user/x"):
            root, source, _problem = studio_profile.resolveRoot({studio_profile.ENVIRONMENT_VARIABLE: value} if value.strip() else {}, self.home, "linux", None, default=default)
            self.assertEqual((root, source), (default, "default"), value)

    def test_Defaults(self):
        self.assertEqual(studio_profile.defaultRoot({}, "/Users/u", "darwin"), "/Users/u/Library/Application Support/slicer.org/Slicer")
        self.assertEqual(studio_profile.defaultRoot({}, "/home/u", "linux"), "/home/u/.local/share/slicer.org/Slicer")
        self.assertEqual(studio_profile.defaultRoot({"XDG_DATA_HOME": "/data"}, "/home/u", "linux"), "/data/slicer.org/Slicer")
        self.assertEqual(studio_profile.defaultRoot({"XDG_DATA_HOME": "rel"}, "/home/u", "linux"), "/home/u/.local/share/slicer.org/Slicer")

    def test_RefusedOverrides(self):
        app = self.path("Applications", "Slicer Studio.app")
        contents = os.path.join(app, "Contents")
        os.makedirs(contents)
        forbidden = studio_profile.applicationRoots(contents, "darwin")
        self.assertIn(os.path.realpath(app), forbidden)
        self.assertTrue(any(path.endswith(".Slicer Studio.app.studio-previous") for path in forbidden))
        for bad in (os.path.join(contents, "profile"), self.path("Other.app", "p"), self.path("Applications"),
                    os.path.join(os.path.dirname(app), ".Slicer Studio.app.studio-previous", "x")):
            problem = studio_profile.validateOverride(bad, forbidden)
            self.assertIsNotNone(problem, bad)
            root, source, reason = studio_profile.resolveRoot({studio_profile.ENVIRONMENT_VARIABLE: bad}, self.home, "darwin", None, forbidden, default=self.path("d"))
            self.assertEqual((root, source), (self.path("d"), "default"))
            self.assertIn(studio_profile.ENVIRONMENT_VARIABLE, reason)
        linuxHome = self.path("opt", "SlicerStudio")
        os.makedirs(linuxHome)
        self.assertIsNotNone(studio_profile.validateOverride(os.path.join(linuxHome, "p"), studio_profile.applicationRoots(linuxHome, "linux")))
        self.assertIsNone(studio_profile.validateOverride(self.path("good", "profile"), forbidden))
        afile = self.path("file")
        open(afile, "w").close()
        self.assertIsNotNone(studio_profile.validateOverride(afile))
        if os.geteuid() != 0:
            locked = self.path("locked")
            os.makedirs(locked)
            os.chmod(locked, 0o500)
            self.addCleanup(os.chmod, locked, 0o700)
            self.assertIsNotNone(studio_profile.validateOverride(os.path.join(locked, "p")))

    def test_ApplicationResolution(self):
        """root() in the application: the environment variable, then Slicer.ini, then Qt's AppLocalDataLocation; with
        --disable-settings (``-tmp``) never an override."""
        default = self.path("qtdefault")
        settings = FakeSettings(values={studio_profile.DIRECTORY_KEY: self.path("chosen")})
        app = fakeApp(self.path("install"), 1, self.path("cfg", "SlicerStudio-1.ini"), settings, FakeSettings())
        with mock.patch.dict(sys.modules, {"qt": fakeQt(default)}), mock.patch("studio_profile._application", lambda: app):
            with mock.patch.dict(os.environ, {studio_profile.ENVIRONMENT_VARIABLE: self.path("env")}):
                studio_profile.setRoot(None)
                self.assertEqual(studio_profile.root(), self.path("env"))
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(studio_profile.ENVIRONMENT_VARIABLE, None)
                studio_profile.setRoot(None)
                self.assertEqual(studio_profile.root(), self.path("chosen"))
                self.assertEqual(studio_profile.rootSource(), ("setting", None))
                app.applicationName = "Slicer-tmp"
                studio_profile.setRoot(None)
                self.assertEqual(studio_profile.root(), default)
            self.assertEqual(settings.writes, [], "resolving the root writes nothing")


class DerivedFoldersTest(_Base):
    def test_EveryFolderIsInTheProfile(self):
        profile = self.path("profile")
        studio_profile.setRoot(profile)
        self.assertEqual(profile_legacy.profileRoot(), profile)
        saved = updates.stateDirectoryOverride
        updates.stateDirectoryOverride = None
        self.addCleanup(setattr, updates, "stateDirectoryOverride", saved)
        self.assertEqual(updates.stateDirectory(), os.path.join(profile, "StudioUpdate"))
        self.assertEqual(studio_overlay.stateRootFromApplication(), os.path.join(profile, "StudioUpdate", "commit"))
        # autosave_io and projects need Qt and Slicer to import: their source must take the folder from the profile
        for name, function in (("autosave_io.py", "def builtinRoot"), ("projects.py", "def moveJournalPath")):
            with open(os.path.join(SOURCE_DIR, "Modules", "Scripted", "Studio", "StudioLib", name), encoding="utf-8") as f:
                text = f.read()
            body = text[text.index(function):]
            body = body[: body.index("\ndef ", 1)]
            self.assertIn("profile_legacy.profileRoot()", body, name)
            self.assertNotIn("AppLocalDataLocation", body, name)

    def test_OverlayFollowsEnvironment(self):
        app = fakeApp(self.path("install"), 1, self.path("cfg", "SlicerStudio-1.ini"), FakeSettings(), FakeSettings())
        with mock.patch.dict(sys.modules, {"qt": fakeQt(self.path("qtdefault"))}), mock.patch("studio_profile._application", lambda: app), \
                mock.patch.dict(os.environ, {studio_profile.ENVIRONMENT_VARIABLE: self.path("env")}):
            studio_profile.setRoot(None)
            self.assertEqual(studio_overlay.stateRootFromApplication(), os.path.join(self.path("env"), "StudioUpdate", "commit"))


class CandidatesTest(_Base):
    def test_Discovery(self):
        config = os.path.join(self.home, ".config", "slicer.org")
        profile = self.path("profile")
        # macOS: the installed bundle and the copy an update keeps
        apps = self.path("Applications")
        contents = os.path.join(apps, "Slicer Studio.app", "Contents")
        writeIni(os.path.join(contents, "slicer.org", "Slicer-6303.ini"), {"Modules/AdditionalPaths": "x"})
        os.makedirs(os.path.join(contents, "Extensions-6303"))
        previous = os.path.join(apps, ".Slicer Studio.app.studio-previous", "Contents")
        writeIni(os.path.join(previous, "slicer.org", "Slicer-6290.ini"), {})
        writeIni(os.path.join(config, "SlicerStudio-6300.ini"), {})
        writeIni(os.path.join(config, "SlicerStudio-6310.ini"), {})  # the current revision: excluded
        writeIni(os.path.join(config, "Slicer-6303.ini"), {})  # official Slicer: never read
        snapshot = os.path.join(profile, "StudioLegacy", "6200-abcdef0")
        writeIni(os.path.join(snapshot, "Slicer-6200.ini"), {})
        with open(os.path.join(snapshot, "snapshot.json"), "w") as f:
            json.dump({"format": 1, "revision": "6200", "ini": "Slicer-6200.ini", "home": "/old/Contents"}, f)
        times = {"Slicer-6303.ini": 50, "Slicer-6290.ini": 40, "SlicerStudio-6300.ini": 60, "Slicer-6200.ini": 30}
        for folder, _dirs, files in os.walk(self.tempDir):
            for name in files:
                if name in times:
                    os.utime(os.path.join(folder, name), (times[name], times[name]))
        found = studio_profile.findRevisionCandidates([config], profile, contents, "6310", "darwin")
        self.assertEqual([(entry["kind"], entry["revision"]) for entry in found],
                         [("settings", "6300"), ("installed", "6303"), ("backup", "6290"), ("snapshot", "6200")])
        self.assertEqual(found[1]["extensions"], os.path.join(apps, "Slicer Studio.app", "Contents", "Extensions-6303"))
        self.assertEqual(found[3]["home"], "/old/Contents")
        # Linux: Slicer Studio folders next to this one (with studio_build_info.json); others are never read
        opt = self.path("opt")
        current = os.path.join(opt, "SlicerStudio-new")
        makeStudioHome(current)
        sibling = os.path.join(opt, "SlicerStudio-old")
        makeStudioHome(sibling)
        writeIni(os.path.join(sibling, "slicer.org", "Slicer-100.ini"), {})
        os.makedirs(os.path.join(sibling, "slicer.org", "Extensions-100"))
        official = os.path.join(opt, "Slicer-5.8")
        writeIni(os.path.join(official, "slicer.org", "Slicer-999.ini"), {})
        found = studio_profile.findRevisionCandidates([], "", current, "200", "linux")
        self.assertEqual([(entry["kind"], entry["revision"]) for entry in found], [("sibling", "100")])
        self.assertEqual(found[0]["extensions"], os.path.join(sibling, "slicer.org", "Extensions-100"))


class CarryForwardTest(_Base):
    def ctx(self, exists=None):
        return {"oldHome": "/opt/old", "newHome": "/opt/new", "oldExtensionRoots": ["/opt/old/slicer.org/Extensions-100", "/p/StudioExtensions/k"],
                "newExtensions": "/p/StudioExtensions/k", "exists": exists or (lambda path: not path.endswith("dead"))}

    def test_Rules(self):
        old = {
            "Modules/AdditionalPaths": ["slicer.org/Extensions-100/Foo/lib/Slicer-5.13/qt-scripted-modules", "/opt/old/lib/builtin",
                                        "/home/u/MyModules", "/home/u/dead", "/opt/old/slicer.org/Extensions-100/Bar/lib"],
            "Modules/IgnoreModules": "Sample",
            "Extensions/ManagerEnabled": "true", "Extensions/AutoUpdateCheck": "false", "Extensions/Bookmarked": ["A", "B"],
            "Extensions/ScheduledForUninstall": "Foo", "Extensions/ScheduledForUpdate": "@Variant(x)", "Extensions/MetadataFromServer": "x",
            "Extensions/UpdatesAvailable": "Foo", "Extensions/InstallPath": "/opt/old/slicer.org/Extensions-100",
            "Extensions/ServerUrl": "https://s", "Extensions/FrontendServerUrl": "https://f", "Extensions/LastServerAPI": "Girder_v1",
            "LibraryPaths/size": 2, "LibraryPaths/1/path": "slicer.org/Extensions-100/Foo/lib/Slicer-5.13", "LibraryPaths/2/path": "/gone/dead",
            "EnvironmentVariables/FOO": "1", "General/x": "y", "Application/name": "z", "Python/DockableWindow": "true",
        }
        plan = studio_profile.planCarryForward(old, {}, {}, self.ctx())
        self.assertEqual(plan["Modules/AdditionalPaths"], ["/p/StudioExtensions/k/Foo/lib/Slicer-5.13/qt-scripted-modules", "/home/u/MyModules",
                                                            "/p/StudioExtensions/k/Bar/lib"])
        self.assertEqual(plan["LibraryPaths/size"], 1)
        self.assertEqual(plan["LibraryPaths/1/path"], "/p/StudioExtensions/k/Foo/lib/Slicer-5.13")
        for key in ("Modules/IgnoreModules", "Extensions/ManagerEnabled", "Extensions/AutoUpdateCheck", "Extensions/Bookmarked",
                    "Extensions/ServerUrl", "Extensions/FrontendServerUrl", "Extensions/LastServerAPI", "EnvironmentVariables/FOO"):
            self.assertEqual(plan[key], old[key], key)
        for key in ("Extensions/ScheduledForUninstall", "Extensions/ScheduledForUpdate", "Extensions/MetadataFromServer", "Extensions/UpdatesAvailable",
                    "Extensions/InstallPath", "General/x", "Application/name", "Python/DockableWindow"):
            self.assertNotIn(key, plan)
        # Server URLs only together with the API they were for (the application compares it)
        withoutApi = {key: value for key, value in old.items() if key != "Extensions/LastServerAPI"}
        self.assertNotIn("Extensions/ServerUrl", studio_profile.planCarryForward(withoutApi, {}, {}, self.ctx()))
        # Only keys that are missing or still hold the default are written
        current = {"Extensions/ManagerEnabled": "false", "Extensions/AutoUpdateCheck": "true", "LibraryPaths/size": 1, "LibraryPaths/1/path": "/mine"}
        defaults = {"Extensions/AutoUpdateCheck": "true"}
        plan = studio_profile.planCarryForward(old, current, defaults, self.ctx())
        self.assertNotIn("Extensions/ManagerEnabled", plan)
        self.assertEqual(plan["Extensions/AutoUpdateCheck"], "false")
        self.assertFalse(any(key.startswith("LibraryPaths/") for key in plan))
        # Idempotent: applying the plan and planning again changes nothing
        first = studio_profile.planCarryForward(old, {}, {}, self.ctx())
        self.assertEqual(studio_profile.planCarryForward(old, dict(first), {}, self.ctx()), {})

    def test_MacOSRelativePaths(self):
        ctx = {"oldHome": "/A/Slicer Studio.app/Contents", "newHome": "/A/Slicer Studio.app/Contents", "oldExtensionRoots": [],
               "newExtensions": "/A/Slicer Studio.app/Contents/Extensions-200", "exists": lambda path: True}
        plan = studio_profile.planCarryForward({"Modules/AdditionalPaths": "Extensions-100/Foo/lib/Slicer-5.13/qt-loadable-modules"}, {}, {}, ctx)
        self.assertEqual(plan["Modules/AdditionalPaths"], "Extensions-200/Foo/lib/Slicer-5.13/qt-loadable-modules")


class CopyTest(_Base):
    def tree(self, name="src"):
        source = self.path(name)
        os.makedirs(os.path.join(source, "a", "b"))
        with open(os.path.join(source, "a", "b", "f.txt"), "w") as f:
            f.write("hello")
        with open(os.path.join(source, "big.bin"), "wb") as f:
            f.write(os.urandom(studio_profile.HASH_LIMIT + 10))
        os.symlink("a/b/f.txt", os.path.join(source, "link"))
        return source

    def digest(self, folder):
        found = {}
        for root, _dirs, files in os.walk(folder):
            for name in files:
                path = os.path.join(root, name)
                found[os.path.relpath(path, folder)] = os.readlink(path) if os.path.islink(path) else studio_profile._sha256(path)
        return found

    def test_CopyTreeVerified(self):
        source = self.tree()
        before = self.digest(source)
        for module in (studio_profile, profile_legacy):
            destination = self.path(f"dst-{module.__name__}")
            self.assertIsNone(module.copyTreeVerified(source, destination))
            self.assertIsNone(module.verifyTree(source, destination))
            self.assertTrue(os.path.islink(os.path.join(destination, "link")))
            self.assertEqual(module.copyTreeVerified(source, destination), "the destination exists", "a rerun changes nothing")
        self.assertEqual(self.digest(source), before, "the source is never changed")

    def test_MismatchLeavesNothing(self):
        source = self.tree()
        before = self.digest(source)

        def lossyClone(src, dst, platform=None):
            shutil.copytree(src, dst, symlinks=True)
            os.remove(os.path.join(dst, "a", "b", "f.txt"))

        destination = self.path("dst")
        problem = studio_profile.copyTreeVerified(source, destination, clone=lossyClone)
        self.assertIn("f.txt", problem)
        self.assertFalse(os.path.exists(destination))
        self.assertEqual([name for name in os.listdir(self.tempDir) if ".partial-" in name], [])
        self.assertEqual(self.digest(source), before)

    def test_CloneFallsBack(self):
        source = self.tree()

        def failing(command, **kwargs):
            os.makedirs(command[-1], exist_ok=True)  # a half copy
            return types.SimpleNamespace(returncode=1)

        destination = self.path("dst")
        studio_profile.cloneTree(source, destination, "darwin", run=failing)
        self.assertIsNone(studio_profile.verifyTree(source, destination))


class SnapshotTest(_Base):
    def oldInstall(self, revision="100", commit="b" * 40, platform="linux"):
        if platform == "darwin":
            home = self.path("Apps", "Slicer Studio.app", "Contents")
            extensions = os.path.join(home, f"Extensions-{revision}")
        else:
            home = self.path("opt", f"SlicerStudio-{revision}")
            extensions = os.path.join(home, "slicer.org", f"Extensions-{revision}")
        makeStudioHome(home, commit)
        writeIni(os.path.join(home, "slicer.org", f"Slicer-{revision}.ini"), {"Modules/AdditionalPaths": f"slicer.org/Extensions-{revision}/Foo/lib"})
        makeExtension(extensions, "Foo")
        site = os.path.join(home, "lib", "Python", "lib", "python3.12", "site-packages")
        os.makedirs(os.path.join(site, "numpy-2.0.0.dist-info"))
        built = os.stat(os.path.join(home, "bin", "Python", "studio_build_info.json")).st_mtime
        os.utime(os.path.join(site, "numpy-2.0.0.dist-info"), (built, built))
        os.makedirs(os.path.join(site, "nibabel-5.2.0.dist-info"))
        later = built + 3600
        os.utime(os.path.join(site, "nibabel-5.2.0.dist-info"), (later, later))
        return home, extensions

    def test_SnapshotAndRerun(self):
        home, extensions = self.oldInstall()
        profile = self.path("profile")
        plenty = lambda path: 100 * 1024 ** 3  # noqa: E731
        folder = profile_legacy.snapshot(home, profile, freeBytesFn=plenty, platform="linux")
        self.assertEqual(os.path.basename(folder), "100-bbbbbbb")
        data = loadJson(os.path.join(folder, "snapshot.json"))
        self.assertTrue(data["extensionsCopied"])
        self.assertEqual(data["extensions"], ["Foo"])
        self.assertEqual(data["packages"], [{"name": "nibabel", "version": "5.2.0"}])
        self.assertIsNone(profile_legacy.verifyTree(extensions, os.path.join(folder, "Extensions")))
        self.assertTrue(os.path.isfile(os.path.join(folder, "Slicer-100.ini")))
        created = data["created"]
        self.assertEqual(profile_legacy.snapshot(home, profile, freeBytesFn=plenty, platform="linux", now=created + 10), folder)
        self.assertEqual(loadJson(os.path.join(folder, "snapshot.json"))["created"], created, "unchanged source: no new copy")
        # A newer source is copied again
        later = time.time() + 100
        os.utime(os.path.join(home, "slicer.org", "Slicer-100.ini"), (later, later))
        profile_legacy.snapshot(home, profile, freeBytesFn=plenty, platform="linux", now=created + 20)
        self.assertEqual(loadJson(os.path.join(folder, "snapshot.json"))["created"], created + 20)

    def test_LowSpaceKeepsNames(self):
        home, _extensions = self.oldInstall()
        folder = profile_legacy.snapshot(home, self.path("profile"), freeBytesFn=lambda path: profile_legacy.RESERVED_FREE_BYTES, platform="linux")
        data = loadJson(os.path.join(folder, "snapshot.json"))
        self.assertFalse(data["extensionsCopied"])
        self.assertEqual(loadJson(os.path.join(folder, "extensions.json"))["extensions"], ["Foo"])
        self.assertFalse(os.path.exists(os.path.join(folder, "Extensions")))

    def test_KeepsNewestTwo(self):
        profile = self.path("profile")
        plenty = lambda path: 100 * 1024 ** 3  # noqa: E731
        for index, revision in enumerate(("100", "101", "102")):
            home, _extensions = self.oldInstall(revision, commit=str(index) * 40)
            profile_legacy.snapshot(home, profile, freeBytesFn=plenty, platform="linux", now=1000 + index)
        self.assertEqual(sorted(os.listdir(os.path.join(profile, "StudioLegacy"))), ["101-1111111", "102-2222222"])

    def test_MacOSLocations(self):
        home, extensions = self.oldInstall(platform="darwin")
        self.assertEqual(profile_legacy.inAppLocations(home, "darwin")[0]["extensions"], extensions)
        folder = profile_legacy.snapshot(home, self.path("profile"), freeBytesFn=lambda path: 100 * 1024 ** 3, platform="darwin")
        self.assertTrue(os.path.isfile(os.path.join(folder, "Extensions", "Foo.s4ext")))
        self.assertEqual(profile_legacy.homeOfBackup("/A/.S.app.studio-previous", "darwin"), "/A/.S.app.studio-previous/Contents")

    def test_NothingInsideTheApplication(self):
        home = self.path("opt", "new")
        makeStudioHome(home)
        self.assertIsNone(profile_legacy.snapshot(home, self.path("profile"), platform="linux"))


@unittest.skipIf("slicer" in sys.modules or sys.platform == "darwin", "forks the process: plain Python on Linux only")
class ConcurrencyTest(_Base):
    def test_OneCopies(self):
        source = self.path("src")
        os.makedirs(source)
        with open(os.path.join(source, "f"), "w") as f:
            f.write("x" * 1000)
        profile = self.path("profile")
        context = multiprocessing.get_context("fork")
        barrier, results = context.Barrier(2), context.Queue()
        workers = [context.Process(target=_lockWorker, args=(profile, source, barrier, results, 1.0)) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        outcomes = sorted(results.get(timeout=5) for _ in workers)
        self.assertEqual(outcomes, ["copied", "skipped"])
        self.assertIsNone(studio_profile.verifyTree(source, os.path.join(profile, "copied")))

    def test_LockOfDeadProcessIsFree(self):
        profile = self.path("profile")
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        holder = context.Process(target=_holdLock, args=(profile, ready))
        holder.start()
        self.assertTrue(ready.wait(10))
        with studio_profile.ProfileLock(profile) as lock:
            self.assertFalse(lock.acquired, "held by a live process")
        os.kill(holder.pid, signal.SIGKILL)
        holder.join(10)
        with studio_profile.ProfileLock(profile) as lock:
            self.assertTrue(lock.acquired, "released when its process died")


class ExtensionsTest(_Base):
    def test_Plan(self):
        store = {"A": b"1", "B": b"2"}
        self.assertEqual(studio_profile.planExtensionsSync(store, {}, False)["restore"], ["A", "B"])
        self.assertEqual(studio_profile.planExtensionsSync(store, {"A": b"1"}, False)["restore"], ["B"])
        self.assertEqual(studio_profile.planExtensionsSync(store, dict(store), True)["action"], "noop")
        plan = studio_profile.planExtensionsSync(store, {"A": b"1b", "C": b"3"}, True)
        self.assertEqual((plan["action"], plan["capture"], plan["trash"]), ("capture", ["A", "C"], ["B"]))

    def test_SyncBundleNeverDeletes(self):
        store, bundle, trash = self.path("store"), self.path("bundle"), self.path("trash")
        makeExtension(store, "A")
        makeExtension(store, "B")
        plan = studio_profile.syncBundle(store, bundle, trash, platform="linux", freeBytesFn=lambda path: 100 * 1024 ** 3)
        self.assertEqual((plan["action"], sorted(plan["done"])), ("restore", ["A", "B"]))
        self.assertEqual(sorted(studio_profile.extensionDescriptions(bundle)), ["A", "B"])
        self.assertEqual(studio_profile.syncBundle(store, bundle, trash, platform="linux")["action"], "noop")
        # The user uninstalls B and installs C, and A is updated
        shutil.rmtree(os.path.join(bundle, "B"))
        os.remove(os.path.join(bundle, "B.s4ext"))
        makeExtension(bundle, "C")
        with open(os.path.join(bundle, "A", "lib", "Slicer-5.13", "qt-scripted-modules", "A.py"), "wb") as f:
            f.write(b"new")
        with open(os.path.join(bundle, "A.s4ext"), "a") as f:
            f.write("updated 1\n")
        plan = studio_profile.syncBundle(store, bundle, trash, platform="linux", now=time.time())
        self.assertEqual((plan["capture"], plan["trash"], plan["problems"]), (["A", "C"], ["B"], {}))
        self.assertEqual(studio_profile.extensionDescriptions(store), studio_profile.extensionDescriptions(bundle))
        trashed = os.listdir(trash)
        self.assertEqual(len(trashed), 1)
        kept = sorted(os.listdir(os.path.join(trash, trashed[0])))
        self.assertEqual(kept, ["A", "A.s4ext", "B", "B.s4ext"], "the old A and the uninstalled B are kept in the trash")
        self.assertEqual(studio_profile.purgeTrash(trash, now=time.time()), [])
        self.assertEqual(studio_profile.purgeTrash(trash, now=time.time() + 31 * 86400), trashed)

    def test_ImportOnlyCompatible(self):
        old = self.path("old")
        makeExtension(old, "Good")
        makeExtension(old, "OtherAbi", revision="34000")
        makeExtension(old, "OtherOs", os_="macosx")
        store = self.path("store")
        result = studio_profile.importExtensions([{"extensions": None}, {"extensions": old}], store, SERVER_REVISION, "linux", "amd64",
                                                 freeBytesFn=lambda path: 100 * 1024 ** 3, platform="linux")
        self.assertEqual((result["copied"], sorted(result["skipped"])), (["Good"], ["OtherAbi", "OtherOs"]))
        self.assertEqual(studio_profile.importExtensions([{"extensions": old}], store, SERVER_REVISION, "linux", "amd64")["source"], None,
                         "a store with extensions is never filled again")
        empty = self.path("store2")
        result = studio_profile.importExtensions([{"extensions": old}], empty, SERVER_REVISION, "linux", "amd64",
                                                 freeBytesFn=lambda path: studio_profile.RESERVED_FREE_BYTES)
        self.assertEqual(result["problems"], {"Good": "not enough free space"})


class PythonPackagesTest(_Base):
    def test_PipArguments(self):
        self.assertEqual(studio_profile.pipArguments(["install", "nibabel"]), ["install", "--user", "nibabel"])
        for args in (["install", "--user", "x"], ["install", "--target", "/t", "x"], ["install", "-t", "/t", "x"], ["install", "-t/t", "x"],
                     ["install", "--target=/t", "x"], ["install", "--prefix", "/p", "x"], ["install", "--root=/r", "x"], ["uninstall", "x", "--yes"]):
            self.assertEqual(studio_profile.pipArguments(args), args)

    def test_EnvironmentAndSite(self):
        base = studio_profile.pythonUserBase(self.path("p"), cacheTag="cpython-312", platform="darwin", machine="arm64")
        self.assertEqual(base, self.path("p", "StudioPython", "cpython-312-darwin-arm64"))
        self.assertTrue(studio_profile.userSitePackages(base).startswith(base + os.sep))
        self.assertIn(sys.implementation.cache_tag, studio_profile.pythonUserBase(self.path("p")))
        changes = studio_profile.profileEnvironment({"PYTHONPATH": "/a"}, base, "/site")
        self.assertEqual(changes, {"SLICER_STUDIO_PYTHON_USERBASE": base, "PYTHONUSERBASE": base, "PYTHONPATH": os.pathsep.join(["/a", "/site"])})
        self.assertEqual(studio_profile.profileEnvironment({"PYTHONPATH": "/site"}, base, "/site")["PYTHONPATH"], "/site")
        site = self.path("site")
        os.makedirs(os.path.join(site, "extra"))
        with open(os.path.join(site, "x.pth"), "w") as f:
            f.write("extra\nimport os; os.abort()\n# comment\n")
        sysPath = ["/app/lib/python3.12", "/app/lib/python3.12/site-packages", "/app/other"]
        added = studio_profile.addUserSite(site, sysPath)
        self.assertEqual(added, [site, os.path.join(site, "extra")])
        self.assertEqual(sysPath[1:3], added, "before the bundled site-packages, as Python puts the user site")
        self.assertEqual(studio_profile.addUserSite(site, sysPath), [], "added once")

    def test_PackagingUsesProfile(self):
        with open(os.path.join(SOURCE_DIR, "Base", "Python", "slicer", "packaging.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("return _studioProfileArgs(args)", text)
        self.assertIn("SLICER_STUDIO_PYTHON_USERBASE", text)


class InstallerSafetyTest(_Base):
    def test_RemovalRefused(self):
        profile = self.path("data", "profile")
        os.makedirs(profile)
        protected = [profile, os.path.join(self.home, ".config", "slicer.org")]
        for path in (profile, os.path.join(profile, "StudioAutosave"), self.path("data"), self.tempDir, self.home):
            with self.assertRaises(OSError, msg=path):
                updates_install._removeTree(path, protected)
        self.assertTrue(os.path.isdir(profile))
        errors = []
        updates_install.removeInBackground(self.path("data"), lambda path, error: errors.append(error), protected).join(10)
        self.assertIsInstance(errors[0], OSError)
        self.assertTrue(os.path.isdir(profile))
        other = self.path("app-copy")
        os.makedirs(other)
        updates_install._removeTree(other, protected)
        self.assertFalse(os.path.exists(other))

    def test_HelperRefusesToReplaceUserData(self):
        apps = self.path("apps")
        target = os.path.join(apps, "SlicerStudio")
        staged = os.path.join(apps, ".SlicerStudio.studio-update", "SlicerStudio")
        os.makedirs(os.path.join(target, "profile"))
        os.makedirs(os.path.join(staged, "bin"))
        open(os.path.join(staged, "bin", "SlicerApp-real"), "w").close()
        backup = os.path.join(apps, ".SlicerStudio.studio-previous")
        script = self.path("helper.sh")
        with open(script, "w") as f:
            f.write(updates_install.helperScript([os.path.join(target, "profile"), "/nonexistent/other"]))
        result = self.path("result.txt")
        process = subprocess.run(["/bin/sh", script, "999999", target, staged, backup, result, "0", "linux", "bin/SlicerApp-real"],
                                 capture_output=True, text=True, timeout=60)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("user data; nothing was changed", readText(result))
        self.assertTrue(os.path.isdir(os.path.join(target, "profile")))
        self.assertTrue(os.path.isdir(staged))
        self.assertFalse(os.path.exists(backup))
        # Without user data inside, the same swap goes through
        with open(script, "w") as f:
            f.write(updates_install.helperScript(["/nonexistent/other"]))
        process = subprocess.run(["/bin/sh", script, "999999", target, staged, backup, result, "0", "linux", "bin/SlicerApp-real"],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(readText(result).strip(), "ok")

    def test_GarbageCollectionStaysInside(self):
        self.assertTrue(updates_commit._isStrictlyInside(self.path("a", "b"), self.path("a")))
        self.assertFalse(updates_commit._isStrictlyInside(self.path("a"), self.path("a")))
        self.assertFalse(updates_commit._isStrictlyInside(self.path("ab"), self.path("a")))
        os.symlink(self.home, self.path("a-link"))
        self.assertFalse(updates_commit._isStrictlyInside(self.path("a-link"), self.path("a")))


class StartupTest(_Base):
    """``startup()`` end to end with a fake application, Qt and build configuration."""

    def setUp(self):
        _Base.setUp(self)
        self.profile = self.path("home", ".local", "share", "slicer.org", "Slicer")
        self.config = os.path.join(self.home, ".config", "slicer.org")
        patches = [mock.patch.dict(os.environ, {"HOME": self.home}), mock.patch.dict(sys.modules, {
            "qt": fakeQt(self.profile), "studio_build_config": types.SimpleNamespace(EXTENSIONS_SERVER_REVISION=SERVER_REVISION)})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("XDG_DATA_HOME", None)
        os.environ.pop(studio_profile.ENVIRONMENT_VARIABLE, None)

    def start(self, home, revision, platform, os_="linux"):
        revisionFile = os.path.join(self.config, f"SlicerStudio-{revision}.ini")
        userSettings = FakeSettings(values={studio_profile.PIP_KEY: "false"})
        revisionSettings = FakeSettings(revisionFile)
        app = fakeApp(home, revision, revisionFile, userSettings, revisionSettings, os_=os_)
        studio_profile.setRoot(None)
        with mock.patch("studio_profile._application", lambda: app):
            state = studio_profile.startup(app, platform=platform)
        return app, userSettings, revisionSettings, state

    def test_LinuxNewPackageNextToOld(self):
        old = self.path("opt", "SlicerStudio-old")
        makeStudioHome(old)
        writeIni(os.path.join(old, "slicer.org", "Slicer-100.ini"), {
            "Modules/AdditionalPaths": "slicer.org/Extensions-100/Foo/lib/Slicer-5.13/qt-scripted-modules",
            "LibraryPaths/size": 1, "LibraryPaths/1/path": "slicer.org/Extensions-100/Foo/lib/Slicer-5.13",
            "Extensions/ScheduledForUninstall": "Bar", "Extensions/ManagerEnabled": "true"})
        makeExtension(os.path.join(old, "slicer.org", "Extensions-100"), "Foo")
        new = self.path("opt", "SlicerStudio-new")
        makeStudioHome(new, "c" * 40)
        _app, userSettings, settings, state = self.start(new, 200, "linux")
        store = os.path.join(self.profile, "StudioExtensions", f"{SERVER_REVISION}-linux-amd64")
        self.assertEqual(state["state"], "migrated")
        self.assertEqual(sorted(studio_profile.extensionDescriptions(store)), ["Foo"])
        self.assertEqual(settings.value("Extensions/InstallPath"), store)
        self.assertEqual(settings.value("Modules/AdditionalPaths"), os.path.join(store, "Foo", "lib", "Slicer-5.13", "qt-scripted-modules"))
        self.assertEqual(settings.value("LibraryPaths/1/path"), os.path.join(store, "Foo", "lib", "Slicer-5.13"))
        self.assertIsNone(settings.value("Extensions/ScheduledForUninstall"))
        self.assertEqual(settings.value("Extensions/ManagerEnabled"), "true")
        self.assertEqual(settings.value(studio_profile.CARRIED_FROM_KEY), "Slicer-100.ini")
        self.assertTrue(settings.value(studio_profile.INITIALIZED_KEY))
        self.assertTrue(settings.value(studio_profile.RESTART_KEY))
        self.assertTrue(all(key.startswith("Studio/") for key in userSettings.writes), userSettings.writes)
        manifest = loadJson(os.path.join(self.profile, "StudioProfile.json"))
        self.assertIn("settings:200", manifest["migrations"])
        self.assertTrue(os.path.isdir(os.path.join(old, "slicer.org", "Extensions-100", "Foo")), "the source is kept")
        # The next start of the same build has nothing to do
        _app, _user, settings2, state = self.start(new, 200, "linux")
        self.assertEqual(state["state"], "ready")
        self.assertEqual(settings2.writes, [])
        # The next build (new revision) carries forward from SlicerStudio-200.ini, with the store unchanged
        newer = self.path("opt", "SlicerStudio-newer")
        makeStudioHome(newer, "d" * 40)
        _app, _user, settings3, state = self.start(newer, 210, "linux")
        self.assertEqual(settings3.value("Modules/AdditionalPaths"), os.path.join(store, "Foo", "lib", "Slicer-5.13", "qt-scripted-modules"))
        self.assertEqual(settings3.value(studio_profile.CARRIED_FROM_KEY), "SlicerStudio-200.ini")

    def test_MacOSDragReplace(self):
        app = self.path("Applications", "Slicer Studio.app")
        contents = os.path.join(app, "Contents")
        makeStudioHome(contents, "b" * 40)
        writeIni(os.path.join(contents, "slicer.org", "Slicer-100.ini"), {
            "Modules/AdditionalPaths": "Extensions-100/Foo/lib/Slicer-5.13/qt-scripted-modules", "Extensions/AutoUpdateCheck": "false"})
        makeExtension(os.path.join(contents, "Extensions-100"), "Foo", os_="macosx")
        # Phase 1: the old build keeps a copy in the profile; then the user drags the new build over it
        profile_legacy.snapshot(contents, self.profile, freeBytesFn=lambda path: 100 * 1024 ** 3, platform="darwin")
        shutil.rmtree(app)
        makeStudioHome(contents, "c" * 40)
        _app, _user, settings, state = self.start(contents, 200, "darwin", os_="macosx")
        bundle = os.path.join(contents, "Extensions-200")
        store = os.path.join(self.profile, "StudioExtensions", f"{SERVER_REVISION}-macosx-amd64")
        self.assertEqual(sorted(studio_profile.extensionDescriptions(bundle)), ["Foo"])
        self.assertEqual(studio_profile.extensionDescriptions(store), studio_profile.extensionDescriptions(bundle))
        self.assertEqual(settings.value("Modules/AdditionalPaths"), "Extensions-200/Foo/lib/Slicer-5.13/qt-scripted-modules")
        self.assertEqual(settings.value("Extensions/AutoUpdateCheck"), "false")
        self.assertTrue(settings.value(studio_profile.RESTART_KEY))
        self.assertEqual(self.start(contents, 200, "darwin", os_="macosx")[3]["state"], "ready")
        # Another rebuild dragged over this one: restored from the store, settings from SlicerStudio-200.ini
        shutil.rmtree(app)
        makeStudioHome(contents, "d" * 40)
        _app, _user, settings, state = self.start(contents, 210, "darwin", os_="macosx")
        self.assertEqual(sorted(studio_profile.extensionDescriptions(os.path.join(contents, "Extensions-210"))), ["Foo"])
        self.assertEqual(settings.value("Modules/AdditionalPaths"), "Extensions-210/Foo/lib/Slicer-5.13/qt-scripted-modules")

    def test_TestingAndTemporarySettingsDoNothing(self):
        home = self.path("opt", "new")
        makeStudioHome(home)
        revisionFile = os.path.join(self.config, "SlicerStudio-200.ini")
        for app in (fakeApp(home, 200, revisionFile, FakeSettings(), FakeSettings(), testing=True),
                    fakeApp(home, 200, revisionFile, FakeSettings(), FakeSettings(), name="Slicer-tmp")):
            self.assertEqual(studio_profile.startup(app, platform="linux")["state"], "skipped")
            self.assertEqual(app.revisionUserSettings().writes, [])
        self.assertFalse(os.path.exists(self.profile))

    def test_PendingDirectoryMovesTheProfile(self):
        os.makedirs(os.path.join(self.profile, "StudioAutosave"))
        open(os.path.join(self.profile, "StudioAutosave", "s"), "w").close()
        target = self.path("moved")
        settings = FakeSettings(values={studio_profile.PENDING_DIRECTORY_KEY: target})
        result = studio_profile.applyPendingDirectory(settings, self.profile, {}, self.home, "linux", [])
        self.assertEqual(result, target)
        self.assertTrue(os.path.isfile(os.path.join(target, "StudioAutosave", "s")))
        self.assertTrue(os.path.isfile(os.path.join(self.profile, "StudioAutosave", "s")), "the old folder is kept")
        self.assertEqual(settings.value(studio_profile.DIRECTORY_KEY), target)
        self.assertNotIn(studio_profile.PENDING_DIRECTORY_KEY, settings.values)
        self.assertTrue(all(key.startswith("Studio/Profile/") for key in settings.writes))
        inside = FakeSettings(values={studio_profile.PENDING_DIRECTORY_KEY: os.path.join(self.profile, "sub")})
        self.assertIsNone(studio_profile.applyPendingDirectory(inside, self.profile, {}, self.home, "linux", []))
        self.assertNotIn(studio_profile.DIRECTORY_KEY, inside.values)


if __name__ == "__main__":
    unittest.main()

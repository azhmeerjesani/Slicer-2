"""Checks of the macOS application bundle: native code only, and one Qt version.

A Qt platform plugin from Homebrew's Qt 6.12 was once packaged next to the official Qt 6.11 frameworks (the application
did not start: "Plugin uses incompatible Qt library"), and the x86_64-only CTK application launcher made macOS warn that
the application needs Rosetta 2. This tests ``Utilities/Scripts/SlicerStudioMacOS/check_bundle_binaries.py`` on
synthetic Mach-O files, the framework mixing check of ``Utilities/Scripts/SlicerBundleCloseDeps.py``, and reads the
build files that keep the build on one Qt and build the launcher natively (plain Python, also on Linux:
``python3 StudioMacBundleCheckTest.py``).
"""

import importlib.util
import os
import shutil
import struct
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "..", ".."))

X86_64 = 0x01000007
ARM64 = 0x0100000C


def read(*parts):
    with open(os.path.join(SOURCE_DIR, *parts), encoding="utf-8") as f:
        return f.read()


def load_module(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SOURCE_DIR, *parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def version(text):
    major, minor, patch = (int(part) for part in text.split("."))
    return (major << 16) | (minor << 8) | patch


def dylib_command(cmd, name, current):
    raw = name.encode() + b"\0"
    size = 24 + len(raw)
    size += (-size) % 8
    return struct.pack("<IIIIII", cmd, size, 24, 2, current, 0x10000) + raw.ljust(size - 24, b"\0")


def thin(cputype, install_id=None, loads=(), payload=b""):
    """A 64-bit little-endian Mach-O with an optional LC_ID_DYLIB and LC_LOAD_DYLIB commands."""
    commands = []
    if install_id is not None:
        commands.append(dylib_command(0xD, install_id[0], version(install_id[1])))
    for name, current in loads:
        commands.append(dylib_command(0xC, name, version(current)))
    body = b"".join(commands)
    header = struct.pack("<IIIIIIII", 0xFEEDFACF, cputype, 0, 6, len(commands), len(body), 0, 0)
    return header + body + payload


def fat(*slices):
    """A universal binary of the given thin slices (each aligned at 4096)."""
    offset = 4096
    table, data = [], b""
    for cputype, blob in slices:
        table.append(struct.pack(">IIIII", cputype, 0, offset + len(data), len(blob), 12))
        data += blob + b"\0" * ((-len(blob)) % 4096)
    header = struct.pack(">II", 0xCAFEBABE, len(slices)) + b"".join(table)
    return header.ljust(4096, b"\0") + data


def plugin_metadata(major, minor):
    return b"....QTMETADATA !" + bytes([1, major, minor, 0]) + b"\xbf\x02cocoa"


class BundleBinariesTest(unittest.TestCase):
    def setUp(self):
        self.checker = load_module("check_bundle_binaries", "Utilities", "Scripts", "SlicerStudioMacOS",
                                   "check_bundle_binaries.py")
        self.tmp = tempfile.mkdtemp(prefix="studio-bundle-check-")
        self.app = os.path.join(self.tmp, "Slicer Studio.app")
        self.addCleanup(shutil.rmtree, self.tmp)

    def write(self, relative, data):
        path = os.path.join(self.app, "Contents", relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def make_good_bundle(self, qt="6.11.2"):
        qtcore = "@rpath/QtCore.framework/Versions/A/QtCore"
        qtgui = "@rpath/QtGui.framework/Versions/A/QtGui"
        self.write("Frameworks/QtCore.framework/Versions/A/QtCore",
                   fat((X86_64, thin(X86_64, (qtcore, qt))), (ARM64, thin(ARM64, (qtcore, qt)))))
        self.write("Frameworks/QtGui.framework/Versions/A/QtGui",
                   thin(ARM64, (qtgui, qt), [(qtcore, qt)]))
        os.symlink("A", os.path.join(self.app, "Contents", "Frameworks", "QtCore.framework", "Versions", "Current"))
        self.write("MacOS/SlicerApp-real", thin(ARM64, None, [("@rpath/Frameworks/QtGui.framework/Versions/A/QtGui", qt)]))
        self.write("bin/PythonSlicer", thin(ARM64, None, [("@rpath/Frameworks/QtCore.framework/Versions/A/QtCore", qt)]))
        self.write("lib/QtPlugins/platforms/libqcocoa.dylib",
                   thin(ARM64, ("@rpath/libqcocoa.dylib", "0.0.0"), [(qtgui, qt)], plugin_metadata(6, 11)))
        # Not Mach-O: a Java class file (0xcafebabe, then its version numbers) and a text file
        self.write("share/Example.class", b"\xca\xfe\xba\xbe\x00\x00\x00\x41" + b"\0" * 64)
        self.write("README.txt", b"Slicer Studio")

    def test_GoodBundlePasses(self):
        self.make_good_bundle()
        problems, warnings, summary = self.checker.check(self.app, "arm64")
        self.assertEqual(problems, [])
        self.assertEqual(warnings, [])
        self.assertEqual(summary["qt_version"], "6.11.2")
        self.assertEqual(summary["qt_plugins"], 1)
        # The Java class file and the Versions/Current symlink are not counted
        self.assertEqual(summary["macho_files"], 5)

    def test_X86OnlyLauncherFails(self):
        self.make_good_bundle()
        self.write("bin/PythonSlicer", thin(X86_64, None, [("@rpath/Frameworks/QtCore.framework/Versions/A/QtCore", "6.11.2")]))
        problems, _warnings, _summary = self.checker.check(self.app, "arm64")
        self.assertEqual(len(problems), 1)
        self.assertIn("bin/PythonSlicer", problems[0])
        self.assertIn("x86_64", problems[0])
        # --allow turns it into a warning; an Intel build accepts it but then misses arm64 elsewhere
        problems, warnings, _summary = self.checker.check(self.app, "arm64", allow=["Contents/bin/*"])
        self.assertEqual(problems, [])
        self.assertEqual(len(warnings), 1)
        problems, _warnings, _summary = self.checker.check(self.app, "x86_64")
        self.assertTrue(any("SlicerApp-real" in problem for problem in problems))

    def test_PluginOfAnotherQtFails(self):
        self.make_good_bundle()
        # Homebrew's Qt 6.12 libqcocoa, its QtGui reference rewritten to the bundled framework
        self.write("lib/QtPlugins/platforms/libqcocoa.dylib",
                   thin(ARM64, ("@rpath/libqcocoa.dylib", "0.0.0"),
                        [("@rpath/Frameworks/QtGui.framework/Versions/A/QtGui", "6.12.0")], plugin_metadata(6, 12)))
        problems, _warnings, _summary = self.checker.check(self.app, "arm64")
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any("references QtGui 6.12.0" in problem for problem in problems))
        self.assertTrue(any("Qt plugin built with Qt 6.12" in problem for problem in problems))
        self.assertEqual(self.checker.main(["--app", self.app, "--arch", "arm64"]), 1)
        self.assertEqual(self.checker.main(["--app", self.app, "--arch", "arm64", "--no-qt", "--allow", "*"]), 0)

    def test_FrameworkOfAnotherQtFails(self):
        self.make_good_bundle()
        self.write("Frameworks/QtGui.framework/Versions/A/QtGui",
                   thin(ARM64, ("@rpath/QtGui.framework/Versions/A/QtGui", "6.12.0")))
        problems, _warnings, _summary = self.checker.check(self.app, "arm64")
        self.assertTrue(any("QtGui.framework" in problem and "is QtGui 6.12.0" in problem for problem in problems),
                        problems)


class CloseDepsMixingTest(unittest.TestCase):
    """SlicerBundleCloseDeps.py stops when one framework name resolves to two different installations."""

    def test_TwoQtInstallationsAreRefused(self):
        closedeps = load_module("SlicerBundleCloseDeps", "Utilities", "Scripts", "SlicerBundleCloseDeps.py")
        tmp = tempfile.mkdtemp(prefix="studio-closedeps-")
        self.addCleanup(shutil.rmtree, tmp)
        app = os.path.join(tmp, "Slicer.app")
        official = os.path.join(tmp, "Qt-official", "lib", "QtGui.framework", "Versions", "A", "QtGui")
        homebrew = os.path.join(tmp, "homebrew", "lib", "QtGui.framework", "Versions", "A", "QtGui")
        executable = os.path.join(app, "Contents", "MacOS", "SlicerApp-real")
        plugin = os.path.join(app, "Contents", "lib", "QtPlugins", "platforms", "libqcocoa.dylib")
        for path in (official, homebrew, executable, plugin):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"\xcf\xfa\xed\xfe" + b"\0" * 28)
        loads = {executable: [official], plugin: [homebrew]}

        def scan(path):
            return loads.get(path, []), [], None

        with mock.patch.object(closedeps, "scan", scan), \
                mock.patch.object(closedeps.subprocess, "run") as run, \
                mock.patch("sys.stderr") as stderr:
            result = closedeps.close_deps(app, [], "lib/Slicer-5.13")
        self.assertFalse(result)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("error:", written)
        self.assertIn("QtGui", written)

    def test_OneQtInstallationPasses(self):
        closedeps = load_module("SlicerBundleCloseDeps", "Utilities", "Scripts", "SlicerBundleCloseDeps.py")
        tmp = tempfile.mkdtemp(prefix="studio-closedeps-")
        self.addCleanup(shutil.rmtree, tmp)
        app = os.path.join(tmp, "Slicer.app")
        official = os.path.join(tmp, "Qt-official", "lib", "QtGui.framework", "Versions", "A", "QtGui")
        executable = os.path.join(app, "Contents", "MacOS", "SlicerApp-real")
        plugin = os.path.join(app, "Contents", "lib", "QtPlugins", "platforms", "libqcocoa.dylib")
        for path in (official, executable, plugin):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"\xcf\xfa\xed\xfe" + b"\0" * 28)
        loads = {executable: [official], plugin: [official]}

        def scan(path):
            return loads.get(path, []), [], None

        with mock.patch.object(closedeps, "scan", scan), mock.patch.object(closedeps.subprocess, "run"), \
                mock.patch("sys.stdout"):
            self.assertTrue(closedeps.close_deps(app, [], "lib/Slicer-5.13"))
        self.assertTrue(os.path.isfile(os.path.join(app, "Contents", "Frameworks", "QtGui.framework", "Versions", "A",
                                                    "QtGui")))


class BuildFilesTest(unittest.TestCase):
    def test_LauncherIsBuiltFromSourceOnMacOS(self):
        launcher = read("SuperBuild", "External_CTKAPPLAUNCHER.cmake")
        self.assertRegex(launcher, r'option\(Slicer_CTKAPPLAUNCHER_FROM_SOURCE "[^"]*" ON\)')
        self.assertIn("set(${proj}_DEPENDENCIES CTKAppLauncherLib)", launcher)
        self.assertIn("set(CTKAppLauncher_DIR ${CTKAppLauncherLib_DIR})", launcher)
        # The from-source branch comes before the download
        self.assertLess(launcher.index("AND Slicer_CTKAPPLAUNCHER_FROM_SOURCE)"), launcher.index("ExternalProject_Add(${proj}"))

    def test_BuildScriptNeedsNoRosetta(self):
        script = read("Utilities", "Scripts", "SlicerStudioMacOS", "build_dmg.sh")
        self.assertNotIn("--install-rosetta", script)
        self.assertIn("check_bundle_binaries.py", script)
        self.assertIn('/usr/bin/arch -arch "${HOST_ARCH}"', script)

    def test_BuildScriptKeepsHomebrewQtOut(self):
        script = read("Utilities", "Scripts", "SlicerStudioMacOS", "build_dmg.sh")
        # Homebrew's whole bin folder (with its qmake) is never put on PATH
        self.assertNotIn('PATH="${BREW_PREFIX}/bin:${PATH}"', script)
        self.assertRegex(script, r'(?m)^BREW_TOOLS="[^"]*"$')
        self.assertNotIn("qt", script.split('BREW_TOOLS="', 1)[1].split('"', 1)[0])
        self.assertIn("export CMAKE_PREFIX_PATH", script)
        self.assertIn('"qt_tools=selected-qt-only"', script)
        self.assertIn("check_qt_packages", script)
        self.assertIn(".smoke-failed", script)

    def test_QtFoldersComeFromTheSelectedQt(self):
        block = read("CMake", "SlicerBlockFindQtAndCheckVersion.cmake")
        self.assertIn('set(QT_PLUGINS_DIR "${QT6_INSTALL_PREFIX}/${QT6_INSTALL_PLUGINS}")', block)
        self.assertIn("slicer_isolate_qt6_packages()", block)
        isolate = read("CMake", "SlicerMacroIsolateQt6Packages.cmake")
        self.assertIn("list(PREPEND CMAKE_PREFIX_PATH", isolate)
        # The packages are isolated before the first Qt 6 lookup, which creates
        # Qt6::qmake, Qt6::moc and Qt6::rcc (Qt6CoreTools).
        top = read("CMakeLists.txt")
        first = top.index("find_package(Qt6 COMPONENTS Core QUIET)")
        self.assertIn("slicer_isolate_qt6_packages()", top[:first])
        self.assertLess(top.index("find_package(Qt6 COMPONENTS Core QUIET)"),
                        top.index("include(SlicerBlockFindQtAndCheckVersion)"))
        self.assertIn("come from another Qt installation or version", block)
        for project in ("CTK", "VTK", "qRestAPI", "CTKAppLauncherLib"):
            with self.subTest(project=project):
                self.assertIn("-DCMAKE_PREFIX_PATH:PATH=${Slicer_QT_PREFIX_PATH}",
                              read("SuperBuild", "External_%s.cmake" % project))


if __name__ == "__main__":
    unittest.main()

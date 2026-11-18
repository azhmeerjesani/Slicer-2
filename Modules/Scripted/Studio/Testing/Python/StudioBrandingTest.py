"""The Slicer Studio name and logo, and the 3D Slicer identifiers that must not change.

The user sees "Slicer Studio" and its own logo (designed in Claude Design, ``Applications/SlicerApp/Resources/Brand``)
in the window title, About box, splash screen, Dock and Finder. The application name ``Slicer`` stays: the settings
files shared with official 3D Slicer (``Slicer.ini``), the launcher and the scene files depend on it. Plain Python,
also on Linux: ``python3 StudioBrandingTest.py``.
"""

import importlib.util
import os
import re
import sys
import unittest
import xml.etree.ElementTree as ET

_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "..", ".."))
BRAND = os.path.join(SOURCE_DIR, "Applications", "SlicerApp", "Resources", "Brand")


def read(*parts):
    with open(os.path.join(SOURCE_DIR, *parts), encoding="utf-8") as f:
        return f.read()


def load_module(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(SOURCE_DIR, *parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assets():
    return load_module("make_brand_assets", "Utilities", "Scripts", "SlicerStudioBrand", "make_brand_assets.py")


class StudioBrandingTest(unittest.TestCase):
    def setUp(self):
        if not os.path.isdir(BRAND):
            self.skipTest("source tree not available: " + BRAND)

    def test_DisplayNameIsSlicerStudioAndApplicationNameStaysSlicer(self):
        text = read("Applications", "SlicerApp", "slicer-application-properties.cmake")
        self.assertRegex(text, r'set\(APPLICATION_DISPLAY_NAME\s+"Slicer Studio"\s*\)')
        # Slicer.ini, SlicerLauncherSettings.ini, Contents/MacOS/Slicer, MRML_APPLICATION_NAME: unchanged
        self.assertRegex(text, r"set\(APPLICATION_NAME\s+Slicer\s*\)")

    def test_SettingsOrganizationIsUnchanged(self):
        text = read("CMake", "SlicerApplicationOptions.cmake")
        self.assertRegex(text, r'set\(Slicer_ORGANIZATION_DOMAIN "slicer\.org" CACHE')
        self.assertRegex(text, r'set\(Slicer_ORGANIZATION_NAME "slicer\.org" CACHE')

    def test_WindowTitleIsTheStudioVersion(self):
        version = load_module("studio_version", "Modules", "Scripted", "Studio", "StudioLib", "version.py")
        self.assertEqual(version.windowTitle(), "Slicer Studio " + version.STUDIO_VERSION)
        self.assertIn("_setWindowTitle()", read("Modules", "Scripted", "Studio", "Studio.py"))
        # Main.cxx no longer appends the 3D Slicer version to "Slicer Studio"
        self.assertNotIn("Slicer_VERSION_FULL);", read("Applications", "SlicerApp", "Main.cxx"))

    def test_AttributionToThreeDSlicerIsKept(self):
        core = read("Base", "QTCore", "qSlicerCoreApplication.cxx")
        self.assertIn("Slicer Studio is based on 3D Slicer", core)
        self.assertIn("https://slicer.readthedocs.io/en/latest/user_guide/about.html#license", core)
        self.assertIn("NOT an FDA approved medical device", core)
        self.assertIn("[3D Slicer](https://www.slicer.org)", read("README.md"))
        self.assertIn("3D Slicer", read("Modules", "Loadable", "SlicerWelcome", "Resources", "UI", "qSlicerWelcomeModuleWidget.ui"))

    def test_MasterLogoIsTheClaudeDesignLogo(self):
        root = ET.parse(os.path.join(BRAND, "SlicerStudioLogo.svg")).getroot()
        self.assertEqual(root.get("viewBox"), "0 0 1024 1024")
        shapes = [(element.tag.split("}")[-1], element.attrib) for element in root if not element.tag.endswith("title")]
        self.assertEqual([tag for tag, _ in shapes], ["rect", "polygon", "polygon", "polygon", "ellipse"])
        self.assertEqual(shapes[0][1]["fill"], "#0f1b33")
        self.assertEqual([attrib.get("fill-opacity", "1") for _, attrib in shapes[1:4]], ["0.35", "0.65", "1"])
        self.assertEqual(shapes[3][1]["stroke"], "#7aa7ff")
        self.assertEqual(shapes[4][1]["fill"], "#ff8a3d")

    def test_VariantsOnlyPlaceOrDropPartsOfTheMaster(self):
        master = read("Applications", "SlicerApp", "Resources", "Brand", "SlicerStudioLogo.svg")
        polygons = re.findall(r"<polygon [^>]*/>", master)
        for name in ("SlicerStudioIcon-macOS.svg", "SlicerStudioMark.svg", "SlicerStudioSplash.svg",
                     "SlicerStudioLockup-horizontal.svg", "SlicerStudioLockup-vertical.svg"):
            text = read("Applications", "SlicerApp", "Resources", "Brand", name)
            for polygon in polygons:
                self.assertIn(re.search(r'points="[^"]+"', polygon).group(0), text, name)
        small = read("Applications", "SlicerApp", "Resources", "Brand", "SlicerStudioLogo-small.svg")
        self.assertNotIn('fill-opacity="0.35"', small)
        self.assertIn('fill-opacity="0.65"', small)

    def test_GeneratedFilesArePresentWithTheirSizes(self):
        module = assets()
        self.assertEqual(module.check(), 0)

    def test_IcnsHoldsOnePngPerSize(self):
        module = assets()
        with open(os.path.join(SOURCE_DIR, module.ICNS_OUTPUT), "rb") as f:
            entries = module.icnsEntries(f.read())
        self.assertEqual(entries[b"ic10"], (1024, 1024))
        self.assertEqual(entries[b"icp4"], (16, 16))
        packed = module.icnsBytes([(b"ic07", b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + bytes(8))])
        self.assertEqual(packed[:4], b"icns")
        self.assertEqual(int.from_bytes(packed[4:8], "big"), len(packed))

    def test_ResourcesReferenceExistingFiles(self):
        qrc = read("Applications", "SlicerApp", "Resources", "SlicerApp.qrc")
        for path in re.findall(r">([^<]+\.png)</file>", qrc):
            self.assertTrue(os.path.isfile(os.path.join(SOURCE_DIR, "Applications", "SlicerApp", "Resources", path)), path)
        webServer = read("Modules", "Scripted", "WebServer", "CMakeLists.txt")
        self.assertIn("SlicerStudio-DesktopIcon.png", webServer)
        self.assertIn("SlicerStudio-DesktopIcon.png", read("Modules", "Scripted", "WebServer", "Resources", "docroot", "index.html"))
        conf = read("Docs", "conf.py")
        for name in re.findall(r'html_(?:logo|favicon) = "([^"]+)"', conf):
            self.assertTrue(os.path.isfile(os.path.join(SOURCE_DIR, "Docs", name)), name)

    def test_LinuxDesktopEntry(self):
        entry = read("Applications", "SlicerApp", "Resources", "Linux", "slicer-studio.desktop.in")
        self.assertIn("\nName=Slicer Studio\n", entry)
        self.assertIn("\nIcon=slicer-studio\n", entry)
        self.assertIn('Exec="@LAUNCHER@" %F', entry)
        self.assertIn("install-desktop-entry.sh", read("Applications", "SlicerApp", "CMakeLists.txt"))

    def test_DiskImageInfoStrings(self):
        script = read("Utilities", "Scripts", "SlicerStudioMacOS", "build_dmg.sh")
        self.assertIn("NSHumanReadableCopyright", script)
        self.assertIn('CFBundleGetInfoString -string "${APP_NAME} ${studio}, based on 3D Slicer ${version}"', script)


if __name__ == "__main__":
    sys.exit(unittest.main())

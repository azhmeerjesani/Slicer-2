"""The names that place the user's data must not change from one build to the next.

The revision-specific settings (``~/.config/slicer.org/SlicerStudio-<rev>.ini``) and the user profile
(``<AppLocalDataLocation>`` = ``.../slicer.org/Slicer``) are found again after a reinstall only while the organization
name and domain, the application name and the settings file base name stay the same, and while the build keeps the
settings outside the application. This reads the build files (plain Python, ``python3 StudioProfileIdentityTest.py``).
"""

import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "..", ".."))


def read(*parts):
    with open(os.path.join(SOURCE_DIR, *parts), encoding="utf-8") as f:
        return f.read()


class ProfileIdentityTest(unittest.TestCase):
    def test_BuildScriptKeepsTheIdentity(self):
        script = read("Utilities", "Scripts", "SlicerStudioMacOS", "build_dmg.sh")
        self.assertRegex(script, r'(?m)^APP_BUNDLE_ID="org\.slicer\.slicerstudio"$')
        self.assertNotIn("-DSlicer_ORGANIZATION_", script)
        self.assertNotIn("-DSlicer_MAIN_PROJECT_APPLICATION_NAME", script)
        self.assertNotRegex(script, r"Slicer_STORE_SETTINGS_IN_APPLICATION_HOME_DIR:BOOL=ON\s*$")
        self.assertIn("-DSlicer_STORE_SETTINGS_IN_APPLICATION_HOME_DIR:BOOL=OFF", script)

    def test_SettingsOptions(self):
        options = read("CMake", "SlicerApplicationOptions.cmake")
        self.assertRegex(options, r'set\(Slicer_ORGANIZATION_DOMAIN "slicer\.org" CACHE')
        self.assertRegex(options, r'set\(Slicer_ORGANIZATION_NAME "slicer\.org" CACHE')
        self.assertRegex(options, r'option\(Slicer_STORE_SETTINGS_IN_APPLICATION_HOME_DIR "[^"]*" OFF\)')
        self.assertRegex(options, r'set\(SLICER_REVISION_SPECIFIC_USER_SETTINGS_FILEBASENAME "Studio"\)')
        # A build tree configured with the old default is turned OFF once
        self.assertIn("set(Slicer_STORE_SETTINGS_IN_APPLICATION_HOME_DIR OFF CACHE BOOL", options)
        properties = read("Applications", "SlicerApp", "slicer-application-properties.cmake")
        self.assertRegex(properties, r"set\(APPLICATION_NAME\s+Slicer\s*\)")

    def test_ProfileModuleIsInstalledAndRunsFirst(self):
        cmake = read("Base", "Python", "CMakeLists.txt")
        scripts = cmake[cmake.index("set(Slicer_PYTHON_SCRIPTS"):]
        scripts = scripts[: scripts.index(")")]
        self.assertIn("studio_profile", scripts.split())
        self.assertIn("studio_build_config.py.in", cmake)
        self.assertIn("@Slicer_EXTENSIONS_SERVER_REVISION@", read("Base", "Python", "studio_build_config.py.in"))
        slicerqt = read("Base", "Python", "slicerqt.py")
        self.assertLess(slicerqt.index("import studio_profile"), slicerqt.index("import studio_overlay"))
        self.assertLess(slicerqt.index("import studio_overlay"), slicerqt.index("\nimport qt"))

    def test_ProfileModuleNeedsANewBuild(self):
        """studio_profile.py runs before a code update from main applies: a change to it waits for a new build."""
        buildInfo = read("Utilities", "Scripts", "SlicerStudioRelease", "make_build_info.py")
        self.assertRegex(buildInfo, r'PRE_OVERLAY = \([^)]*"Base/Python/studio_profile\.py"')
        overlay = read("Base", "Python", "studio_overlay.py")
        self.assertRegex(overlay, r'BOOTSTRAP_MODULES = \([^)]*"studio_profile"')

    def test_NamesInTheProfileModule(self):
        profile = read("Base", "Python", "studio_profile.py")
        self.assertRegex(profile, r'(?m)^ORGANIZATION = "slicer\.org"$')
        self.assertRegex(profile, r'(?m)^APPLICATION = "Slicer"$')
        self.assertRegex(profile, r'(?m)^SETTINGS_BASENAME = "Studio"$')
        self.assertTrue(re.search(r'ENVIRONMENT_VARIABLE = "SLICER_STUDIO_PROFILE_DIR"', profile))


if __name__ == "__main__":
    unittest.main()

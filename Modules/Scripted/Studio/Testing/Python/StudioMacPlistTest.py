"""macOS document types: Launch Services matches only the last path extension (NSURL.pathExtension),
so '*.nii.gz' is seen as 'gz' and 'gz' must be listed for Open With / Dock drops to reach the app.
"""

import os
import plistlib
import re
import unittest



def _plistPath():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, *([os.pardir] * 5)))
    return os.path.join(root, "CMake", "MacOSXBundleInfo.plist.in")


class StudioMacPlistTest(unittest.TestCase):
    def test_NiiGzIsClaimed(self):
        path = _plistPath()
        if not os.path.isfile(path):
            self.skipTest("source tree not available: " + path)
        with open(path, encoding="utf-8") as f:
            text = re.sub(r"\$\{[^}]*\}", "x", f.read())
        info = plistlib.loads(text.encode("utf-8"))
        extensions = set()
        for docType in info["CFBundleDocumentTypes"]:
            extensions.update(e.lower() for e in docType["CFBundleTypeExtensions"])
        for name in ("sub-01_T1w.nii.gz", "lesion.nii", "a.seg.nrrd", "b.mrb", "c.mgz"):
            self.assertIn(os.path.splitext(name)[1][1:].lower(), extensions, name)

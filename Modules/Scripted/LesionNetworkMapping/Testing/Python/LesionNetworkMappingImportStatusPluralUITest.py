"""Test the Lead-DBS import dialog's done message: one lesion map is singular, several are plural.

Bug leaddbs-import-status-1-lesion-maps-plural: onDone said 'Imported 1 lesion maps'.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingImportStatusPluralUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class ImportStatusPluralUITest(unittest.TestCase):
    def setUp(self):
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()
        self.tmp = tempfile.mkdtemp(prefix="lnm-import-plural-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def statusFor(self, n):
        from LesionNetworkMappingUI import manager_parity

        status = []
        fake = SimpleNamespace(
            progress=SimpleNamespace(update=lambda: None),
            updateButtons=lambda: None,
            fillTable=lambda *args: None,
            statusLabel=SimpleNamespace(setText=status.append),
        )
        job = SimpleNamespace(
            succeeded=lambda: True,
            results=[{"kind": "maps", "path": os.path.join(self.tmp, "maps.tsv")}],
            warnings=[],
            doneEvent={"n": n},
        )
        manager_parity.ImportDialog.onDone(fake, job)
        return status[-1]

    def test_one_map_is_singular(self):
        text = self.statusFor(1)
        self.assertIn("Imported 1 lesion map into", text)
        self.assertNotIn("1 lesion maps", text)

    def test_several_maps_are_plural(self):
        self.assertIn("Imported 3 lesion maps into", self.statusFor(3))


if __name__ == "__main__":
    unittest.main()

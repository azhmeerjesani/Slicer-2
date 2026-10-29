"""Test that the "Get a connectome" dialog is clamped to the available screen area.

``qt.QGuiApplication.primaryScreen`` is a property in PythonQt (Qt 5 and Qt 6), not a method: calling it raised a TypeError that
the dialog swallowed, so the clamp of its 1000 x 760 default size never ran and the dialog was as high as a short screen.

It needs the Slicer application (no network is used)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCatalogDialogTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class LesionNetworkMappingCatalogDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm catalog dialog ")
        cls.catalog = os.path.join(cls.tmp, "catalog.json")
        with open(cls.catalog, "w") as handle:
            json.dump({"version": 1, "entries": []}, handle)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def makeDialog(self, area):
        from LesionNetworkMappingUI import catalog_dialog

        with mock.patch.object(catalog_dialog, "_availableArea", return_value=area):
            dialog = catalog_dialog.CatalogDialog(catalogPath=self.catalog)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_available_area_of_the_screen_is_found(self):
        from LesionNetworkMappingUI import catalog_dialog

        area = catalog_dialog._availableArea(slicer.util.mainWindow())
        self.assertIsNotNone(area)
        self.assertGreater(area.width(), 0)
        self.assertGreater(area.height(), 0)
        self.assertIsNotNone(catalog_dialog._availableArea())

    def test_short_screen_clamps_the_default_size(self):
        dialog = self.makeDialog(qt.QRect(0, 0, 1366, 700))
        self.assertEqual((dialog.width, dialog.height), (1000, 600))
        dialog = self.makeDialog(qt.QRect(0, 0, 1366, 728))
        self.assertEqual(dialog.height, 608)

    def test_narrow_screen_clamps_the_width(self):
        dialog = self.makeDialog(qt.QRect(0, 0, 900, 1000))
        self.assertEqual(dialog.width, 860)

    def test_large_screen_keeps_the_default_size(self):
        dialog = self.makeDialog(qt.QRect(0, 0, 2560, 1440))
        self.assertEqual((dialog.width, dialog.height), (1000, 760))

    def test_no_screen_keeps_the_default_size(self):
        dialog = self.makeDialog(None)
        self.assertEqual((dialog.width, dialog.height), (1000, 760))

    def test_failed_terms_fetch_keeps_the_failure_text(self):
        dialog = self.makeDialog(None)
        dialog.entry = {"id": "demo-entry"}
        job = mock.Mock(results=[], failureReason="the network is unreachable")
        job.succeeded.return_value = False
        with mock.patch.object(dialog, "updateState"):
            dialog.onTermsDone(job, "demo-entry")
        text = dialog.termsView.toPlainText()
        self.assertIn("could not be fetched", text)
        self.assertIn("the network is unreachable", text)
        self.assertFalse(dialog.acceptCheck.checked)
        self.assertFalse(dialog.acceptCheck.enabled)
        self.assertEqual(dialog.shaLabel.text, "")
        self.assertTrue(dialog.loadTermsButton.enabled)


if __name__ == "__main__":
    unittest.main()

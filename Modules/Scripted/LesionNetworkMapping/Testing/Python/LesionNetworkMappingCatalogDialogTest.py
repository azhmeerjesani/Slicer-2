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

    def test_primary_button_does_not_offer_resume_while_running_or_after_a_mismatch(self):
        from LesionNetworkMappingLib import catalog, fetchconnectome, jobs
        from LesionNetworkMappingUI import catalog_dialog, manager_catalog

        dialog = self.makeDialog(None)
        entry = catalog.load_catalog().get("gsp1000-ilxiks")
        dest = os.path.join(self.tmp, "dest")
        work = fetchconnectome.work_dir(entry, dest)

        def writeMismatchState():
            os.makedirs(work, exist_ok=True)
            jobs.write_json_atomic(os.path.join(work, "state.json"), {"format": fetchconnectome.STATE_FORMAT, "version": 1, "entry": entry["id"], "sizes": {"a.tar": 100},
                                                                      "archives": {"a.tar": {"status": "mismatch", "bytes": 100}}, "files": {}, "complete": False})

        writeMismatchState()
        dialog.entry = entry
        dialog.destEdit.setText(dest)
        # The terms are read and accepted and the plan fits, so that only the fetch state decides whether the button is enabled
        dialog.terms[entry["id"]] = {"text": "terms", "display": "terms", "sha": "a" * 64}
        wasBlocked = dialog.acceptCheck.blockSignals(True)  # the tick is not stored in the settings
        dialog.acceptCheck.setChecked(True)
        dialog.acceptCheck.blockSignals(wasBlocked)
        for patcher in (mock.patch.object(manager_catalog, "termsAccepted", return_value=True), mock.patch.object(catalog_dialog, "planSummary", return_value=("", None))):
            patcher.start()
            self.addCleanup(patcher.stop)
        controller = dialog.controller
        saved = (controller.state, controller.request, controller.errorEvent, controller.job)
        self.addCleanup(lambda: (setattr(controller, "state", saved[0]), setattr(controller, "request", saved[1]), setattr(controller, "errorEvent", saved[2]),
                                 setattr(controller, "job", saved[3])))
        controller.job = None
        controller.state = "idle"
        controller.errorEvent = None
        dialog.updateState()
        self.assertEqual(dialog.downloadButton.text, "Resume this download")  # an interrupted fetch is resumable
        self.assertTrue(dialog.downloadButton.enabled)
        # A failed fetch on a checksum mismatch: nothing to resume, the same archive would fail again
        controller.request = {"entry": entry["id"], "dest": dest}
        controller.state = "failed"
        controller.errorEvent = {"code": "checksum"}
        self.assertTrue(dialog.mismatchHere())
        dialog.updateState()
        self.assertEqual(dialog.downloadButton.text, "Download and convert")
        self.assertFalse(dialog.downloadButton.enabled)
        self.assertIn("Keep this data anyway", dialog.hintLabel.text)
        with mock.patch.object(controller, "start") as start:
            self.assertIsNone(dialog.onDownload())
            start.assert_not_called()
        # Another folder is not affected
        dialog.destEdit.setText(os.path.join(self.tmp, "elsewhere"))
        self.assertFalse(dialog.mismatchHere())
        dialog.destEdit.setText(dest)
        self.assertTrue(dialog.mismatchHere())
        # Deleting the folder (what the failure text suggests) clears it in the same session: a new download may start
        shutil.rmtree(dest)
        self.assertFalse(dialog.mismatchHere())
        dialog.updateState()
        self.assertEqual(dialog.downloadButton.text, "Download and convert")
        self.assertTrue(dialog.downloadButton.enabled, dialog.hintLabel.text)
        writeMismatchState()  # the folder holds the failed download again
        self.assertTrue(dialog.mismatchHere())
        # While the job runs the button is not a Resume button either
        controller.state = "running"
        controller.errorEvent = None
        controller.job = mock.Mock()
        controller.job.isFinished.return_value = False
        dialog.updateState()
        self.assertEqual(dialog.downloadButton.text, "Download and convert")
        self.assertFalse(dialog.downloadButton.enabled)
        # The failed job's own Resume button is hidden for a mismatch and for a changed terms text
        controller.job = None
        for code in ("checksum", "terms_mismatch"):
            controller.state = "failed"
            controller.errorEvent = {"code": code}
            controller.error = "x"
            dialog.onControllerChanged()
            self.assertFalse(dialog.resumeButton.visible, code)
        controller.errorEvent = {"code": "network"}
        dialog.onControllerChanged()
        self.assertTrue(dialog.resumeButton.isVisibleTo(dialog))

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

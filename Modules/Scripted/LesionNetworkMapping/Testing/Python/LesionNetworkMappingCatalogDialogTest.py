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
            bundle = fetchconnectome.bundle_path(entry, dest)  # the subjects converted so far (a state whose bundle is gone is not resumed)
            os.makedirs(bundle, exist_ok=True)
            jobs.write_json_atomic(os.path.join(bundle, "connectome.partial.json"), {"format": "lnmc", "subjects": []})

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

    def test_resume_is_not_offered_for_a_subset_that_leaves_out_converted_archives(self):
        """Paused with All archives when the first archive is done and the second partly converted: choosing 'The first archive'
        does not offer 'Resume this download' (it failed with "missing connectome.json"); a plain hint says why. The same
        subset or a larger one can be resumed, and the controller refuses the narrower one up front too.
        """
        from LesionNetworkMappingLib import catalog, fetchconnectome, jobs
        from LesionNetworkMappingUI import catalog_dialog, manager_catalog

        entry = catalog.load_catalog().get("gsp1000-ilxiks")
        dialog = self.makeDialog(None)
        dest = os.path.join(self.tmp, "dest subset")
        work = fetchconnectome.work_dir(entry, dest)
        os.makedirs(work, exist_ok=True)
        names = [f["name"] for f in catalog.archives(entry)]
        archives = {names[0]: {"status": "done", "bytes": 100}, names[1]: {"status": "partial", "bytes": 40, "memberOffset": 4096, "subjectsDone": 2}}
        jobs.write_json_atomic(os.path.join(work, "state.json"), {"format": fetchconnectome.STATE_FORMAT, "version": 1, "entry": entry["id"], "subset": None,
                                                                  "sizes": dict.fromkeys(names, 100), "archives": archives, "files": {}, "complete": False})
        bundle = fetchconnectome.bundle_path(entry, dest)
        os.makedirs(bundle, exist_ok=True)
        jobs.write_json_atomic(os.path.join(bundle, "connectome.partial.json"), {"format": "lnmc", "subjects": []})
        dialog.entry = entry
        dialog.fillFetchBox(entry)
        dialog.destEdit.setText(dest)
        dialog.terms[entry["id"]] = {"text": "terms", "display": "terms", "sha": "a" * 64}
        wasBlocked = dialog.acceptCheck.blockSignals(True)
        dialog.acceptCheck.setChecked(True)
        dialog.acceptCheck.blockSignals(wasBlocked)
        controller = dialog.controller
        saved = (controller.state, controller.request, controller.errorEvent, controller.job)
        self.addCleanup(lambda: (setattr(controller, "state", saved[0]), setattr(controller, "request", saved[1]), setattr(controller, "errorEvent", saved[2]),
                                 setattr(controller, "job", saved[3])))
        controller.job = None
        controller.state = "idle"
        controller.errorEvent = None
        values = [dialog.subsetCombo.itemData(i) for i in range(dialog.subsetCombo.count)]
        with mock.patch.object(manager_catalog, "termsAccepted", return_value=True), mock.patch.object(catalog_dialog, "planSummary", return_value=("", None)):
            for value, resumable in (("all", True), ("archives:00-00", False), ("archives:00-01", True)):
                dialog.subsetCombo.setCurrentIndex(values.index(value))
                dialog.updateState()
                self.assertEqual(dialog.downloadButton.text, "Resume this download", value)
                self.assertEqual(dialog.downloadButton.enabled, resumable, (value, dialog.hintLabel.text))
                if resumable:
                    self.assertEqual(dialog.hintLabel.text, "", value)
                else:
                    self.assertIn(names[1], dialog.hintLabel.text)
                    self.assertIn("Resume it with the subset it was started with", dialog.hintLabel.text)
            # The progress card's Resume (and the resume offer) reuse a request: a narrower one is refused before any job starts
            request = {"entry": entry["id"], "dest": dest, "subset": "archives:00-00", "termsSha": "a" * 64}
            with mock.patch.object(controller, "entry", return_value=entry), mock.patch.object(manager_catalog, "fetchEnabled", return_value=True), \
                    mock.patch.object(manager_catalog.jobrunner, "instance") as runner:
                self.assertIsNone(controller.start(request, resume=True))
                self.assertIn(names[1], controller.lastRequestError or "")
                runner.assert_not_called()
                self.assertIsNone(controller.checkResumeSubset({**request, "subset": None}))

    def test_an_unknown_bundle_size_still_refuses_a_destination_too_small_for_its_lower_bound(self):
        """GSP1000 for Lead-DBS: the bundle size is known only at conversion, but 4 GB free cannot hold a 219 GB download's bundle
        (at least half of its float32 archives): Download is refused like the NIfTI entry, not left enabled to fill the disk.
        """
        from LesionNetworkMappingLib import catalog
        from LesionNetworkMappingUI import catalog_dialog

        entry = catalog.load_catalog().get("gsp1000-kktjqc")
        plan = catalog.disk_plan(entry, None, False)
        self.assertIsNone(plan["peak"])
        with mock.patch.object(catalog_dialog, "freeBytes", return_value=4_300_000_000):
            text, problem = catalog_dialog.planSummary(entry, "all", False, self.tmp)
            self.assertIn("peak at least", text)
            self.assertIn("Not enough free space here", problem or "")
            self.assertIn("Not enough free space here", catalog_dialog.planSummary(entry, "Full Set", False, self.tmp)[1] or "")
            self.assertIsNone(catalog_dialog.planSummary(entry, "First Subject", False, self.tmp)[1])  # about 0.1 GB fits
        with mock.patch.object(catalog_dialog, "freeBytes", return_value=plan["peak_min"] + 1):
            self.assertIsNone(catalog_dialog.planSummary(entry, "all", False, self.tmp)[1])

    def test_resume_is_judged_by_the_space_still_to_be_written(self):
        """A partly written download compares the free space with what is still to be written, not with the whole peak (the part
        written is no longer free): the main button stays 'Resume this download' and enabled, like the banner's Resume button.
        """
        from LesionNetworkMappingLib import catalog, fetchconnectome, jobs
        from LesionNetworkMappingUI import catalog_dialog, manager_catalog

        entry = catalog.load_catalog().get("gsp1000-ilxiks")
        peak = catalog.disk_plan(entry, None, False)["peak"]
        self.assertTrue(peak)
        free = round(peak * 0.4)
        self.assertLess(free, peak * 1.05)
        # A new download needs the whole peak
        with mock.patch.object(catalog_dialog, "freeBytes", return_value=free):
            problem = catalog_dialog.planSummary(entry, "all", False, self.tmp)[1]
            self.assertIn("at the peak", problem or "")
            # 90 % written: 10 % of the peak is still to come, and it fits
            self.assertIsNone(catalog_dialog.planSummary(entry, "all", False, self.tmp, {"done": 90, "total": 100, "fraction": 0.9})[1])
            # 10 % written: 90 % is still to come, and it does not fit; the hint says how much is still needed
            problem = catalog_dialog.planSummary(entry, "all", False, self.tmp, {"done": 10, "total": 100, "fraction": 0.1})[1]
            self.assertIn("resuming needs about", problem or "")
            # An unknown fraction is judged by the whole peak
            self.assertIn("at the peak", catalog_dialog.planSummary(entry, "all", False, self.tmp, {"done": 0, "total": 0, "fraction": None})[1] or "")

        # The dialog: an interrupted download in the folder, 90 % written
        dialog = self.makeDialog(None)
        dest = os.path.join(self.tmp, "dest resume")
        work = fetchconnectome.work_dir(entry, dest)
        os.makedirs(work, exist_ok=True)
        jobs.write_json_atomic(os.path.join(work, "state.json"), {"format": fetchconnectome.STATE_FORMAT, "version": 1, "entry": entry["id"], "sizes": {"a.tar": 90, "b.tar": 10},
                                                                  "archives": {"a.tar": {"status": "done", "bytes": 90}, "b.tar": {"status": "pending", "bytes": 0}}, "files": {}, "complete": False})
        bundle = fetchconnectome.bundle_path(entry, dest)
        os.makedirs(bundle, exist_ok=True)
        jobs.write_json_atomic(os.path.join(bundle, "connectome.partial.json"), {"format": "lnmc", "subjects": []})
        dialog.entry = entry
        dialog.destEdit.setText(dest)
        dialog.terms[entry["id"]] = {"text": "terms", "display": "terms", "sha": "a" * 64}
        wasBlocked = dialog.acceptCheck.blockSignals(True)
        dialog.acceptCheck.setChecked(True)
        dialog.acceptCheck.blockSignals(wasBlocked)
        controller = dialog.controller
        saved = (controller.state, controller.request, controller.errorEvent, controller.job)
        self.addCleanup(lambda: (setattr(controller, "state", saved[0]), setattr(controller, "request", saved[1]), setattr(controller, "errorEvent", saved[2]),
                                 setattr(controller, "job", saved[3])))
        controller.job = None
        controller.state = "idle"
        controller.errorEvent = None
        with mock.patch.object(manager_catalog, "termsAccepted", return_value=True), mock.patch.object(catalog_dialog, "freeBytes", return_value=free):
            dialog.updateState()
            self.assertEqual(dialog.downloadButton.text, "Resume this download")
            self.assertTrue(dialog.downloadButton.enabled, dialog.hintLabel.text)
            self.assertEqual(dialog.hintLabel.text, "")
            # A resume that really lacks the space is refused up front, with the space still needed
            with mock.patch.object(catalog_dialog, "freeBytes", return_value=round(peak * 0.05)):
                dialog.updateState()
                self.assertFalse(dialog.downloadButton.enabled)
                self.assertIn("resuming needs about", dialog.hintLabel.text)

    def test_licence_chips_fit_their_column(self):
        """The Licence column is sized from the chip cell widgets (the shipped catalog, nothing is fetched): no chip is narrower than its size hint."""
        from LesionNetworkMappingUI import catalog_dialog

        dialog = catalog_dialog.CatalogDialog()
        self.addCleanup(dialog.deleteLater)
        previous = dialog.controller.catalogPath  # the controller is shared: other tests point it at their own catalog
        self.addCleanup(setattr, dialog.controller, "catalogPath", previous)
        dialog.controller.catalogPath = None
        dialog.refresh()
        dialog.show()
        for _ in range(10):
            slicer.app.processEvents()
        table = dialog.table
        self.assertGreater(table.rowCount, 0)
        for row in range(table.rowCount):
            cell = table.cellWidget(row, 1)
            chip = cell.findChildren("QLabel")[0]
            self.assertGreaterEqual(chip.width, chip.sizeHint.width(), "row %d: chip '%s' is clipped" % (row, chip.text))
            self.assertGreaterEqual(table.columnWidth(1), cell.sizeHint.width())

    def test_refreshing_again_leaves_one_licence_chip_per_row(self):
        """Opening Get a connectome… a second time refills the table: the first filling's chips must not stay behind (they were drawn over the titles)."""
        from LesionNetworkMappingUI import catalog_dialog

        dialog = catalog_dialog.CatalogDialog()
        self.addCleanup(dialog.deleteLater)
        previous = dialog.controller.catalogPath
        self.addCleanup(setattr, dialog.controller, "catalogPath", previous)
        dialog.controller.catalogPath = None
        dialog.show()
        dialog.refresh()
        dialog.refresh()
        dialog.resize(dialog.width + 200, dialog.height)
        for _ in range(20):
            slicer.app.processEvents()
        table = dialog.table
        self.assertGreater(table.rowCount, 0)
        chips = [w for w in table.viewport().findChildren("QLabel") if w.isVisible()]
        self.assertEqual(len(chips), table.rowCount)

    def test_the_progress_line_uses_the_singular_for_one_subject(self):
        dialog = self.makeDialog(None)
        base = {"state": "running", "name": "demo", "fraction": 0.09, "archiveCount": 3, "archiveIndex": 1, "eta": None, "bytes": 7, "total": 70, "error": None, "warnings": []}
        texts = {}
        for count in (0, 1, 2):
            with mock.patch.object(dialog.controller, "status", return_value={**base, "subjects": count}), mock.patch.object(dialog.controller, "job", None):
                dialog.onControllerChanged()
            texts[count] = dialog.progress.label.text
        self.assertIn("archive 1 of 3 · 1 subject", texts[1])
        self.assertNotIn("1 subjects", texts[1])
        self.assertTrue(texts[2].endswith("2 subjects"), texts[2])
        self.assertNotIn("subject", texts[0])


    def test_docs_quote_the_checkbox_labels_of_the_dialog(self):
        """lnm_data.md quoted labels ("I accept these data use terms", "keep archives") that the dialog does not use."""
        root = os.path.abspath(__file__)
        for _ in range(6):
            root = os.path.dirname(root)
        docs = {}
        for name in ("lnm_data.md", "lesionnetworkmapping.md"):
            path = os.path.join(root, "Docs", "user_guide", "modules", name)
            if not os.path.isfile(path):
                self.skipTest("no source tree")
            with open(path, encoding="utf-8") as f:
                docs[name] = " ".join(f.read().split())
        dialog = self.makeDialog(None)
        accept = dialog.acceptCheck.text
        keep = dialog.keepCheck.text
        self.assertTrue(accept.startswith("I have read and accept these terms"), accept)
        self.assertTrue(keep.startswith("Also keep the downloaded archives"), keep)
        self.assertIn('"I have read and accept these terms"', docs["lnm_data.md"])
        self.assertIn('"Also keep the downloaded archives"', docs["lnm_data.md"])
        self.assertIn('"I have read and accept these terms"', docs["lesionnetworkmapping.md"])
        self.assertNotIn('tick "I accept these data use terms"', docs["lnm_data.md"])
        self.assertNotIn('"keep archives"', docs["lnm_data.md"])

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


    def test_typed_destination_is_expanded_or_refused_never_resolved_in_the_application(self):
        """'~/…' typed as the destination was passed to the job unchanged; the job runs in the module folder, so the download
        landed in <modules>/~/… inside the application (and a relative path in <modules>/…), while the plan measured the free
        space of the home folder. '~' is expanded now, everywhere the destination is used; a relative path is refused.
        """
        from LesionNetworkMappingLib import catalog, fetchconnectome
        from LesionNetworkMappingUI import catalog_dialog, manager_catalog

        entry = catalog.load_catalog().get("gsp1000-ilxiks")
        dialog = self.makeDialog(None)
        dialog.entry = entry
        dialog.terms[entry["id"]] = {"text": "terms", "display": "terms", "sha": "a" * 64}
        wasBlocked = dialog.acceptCheck.blockSignals(True)
        dialog.acceptCheck.setChecked(True)
        dialog.acceptCheck.blockSignals(wasBlocked)
        controller = dialog.controller
        saved = (controller.state, controller.request, controller.errorEvent, controller.job)
        self.addCleanup(lambda: (setattr(controller, "state", saved[0]), setattr(controller, "request", saved[1]), setattr(controller, "errorEvent", saved[2]),
                                 setattr(controller, "job", saved[3])))
        controller.job = None
        controller.state = "idle"
        controller.errorEvent = None
        home = os.path.expanduser("~")
        expanded = os.path.join(home, "LNM tilde test", "Connectomes")
        for patcher in (mock.patch.object(manager_catalog, "termsAccepted", return_value=True), mock.patch.object(manager_catalog, "setLastDestination")):
            patcher.start()
            self.addCleanup(patcher.stop)
        with mock.patch.object(catalog_dialog, "planSummary", return_value=("plan", None)) as plan:
            dialog.destEdit.setText("  ~/LNM tilde test/Connectomes ")
            dialog.updateState()
            self.assertEqual(plan.call_args[0][3], expanded)  # the plan measures the folder the job writes to
        self.assertTrue(dialog.downloadButton.enabled, dialog.hintLabel.text)
        self.assertEqual(dialog.request()["dest"], expanded)
        with mock.patch.object(controller, "start", return_value=mock.Mock()) as start:
            self.assertIsNotNone(dialog.onDownload())
            self.assertEqual(start.call_args[0][0]["dest"], expanded)
            manager_catalog.setLastDestination.assert_called_with(expanded)
        # The Lib resolves '~' too (a request saved before, a fetch run from the command line), never against its working folder
        self.assertEqual(fetchconnectome.bundle_path(entry, "~/LNM tilde test/Connectomes"), os.path.join(expanded, f"{entry['id']}.lnmc"))
        self.assertEqual(os.path.dirname(fetchconnectome.work_dir(entry, "~/x")), os.path.join(home, "x"))
        self.assertEqual(os.path.dirname(fetchconnectome.lock_path(entry, "~/x")), os.path.join(home, "x"))
        # A relative path is refused with a plain hint: no plan, no job, nothing remembered
        manager_catalog.setLastDestination.reset_mock()
        for text in ("Connectomes here", "./Connectomes", "~not-a-user-LNM/x"):
            dialog.destEdit.setText(text)
            dialog.updateState()
            self.assertIsNone(dialog.destination(), text)
            self.assertFalse(dialog.downloadButton.enabled, text)
            self.assertIn("is not a full path", dialog.hintLabel.text, text)
            self.assertEqual(dialog.planLabel.text, "", text)
            self.assertFalse(dialog.completeHere() or dialog.unfinishedHere() or dialog.resumeSubsetProblem(), text)
            with mock.patch.object(controller, "start") as start:
                self.assertIsNone(dialog.onDownload())
                start.assert_not_called()
        manager_catalog.setLastDestination.assert_not_called()
        # The controller refuses a relative destination itself (Resume of a saved request, the resume offer)
        with mock.patch.object(controller, "entry", return_value=entry), mock.patch.object(manager_catalog, "fetchEnabled", return_value=True), \
                mock.patch.object(manager_catalog.jobrunner, "instance") as runner:
            self.assertIsNone(controller.start({"entry": entry["id"], "dest": "Connectomes here", "termsSha": "a" * 64}))
            self.assertIn("is not a full path", controller.lastRequestError or "")
            runner.assert_not_called()

    def test_destination_that_is_a_file_is_refused_with_a_plain_hint(self):
        """A file typed as the destination left Download enabled and the job failed with a raw '[Errno 17] File exists'."""
        from LesionNetworkMappingLib import catalog
        from LesionNetworkMappingUI import manager_catalog

        entry = catalog.load_catalog().get("gsp1000-ilxiks")
        dialog = self.makeDialog(None)
        dialog.entry = entry
        dialog.terms[entry["id"]] = {"text": "terms", "display": "terms", "sha": "a" * 64}
        wasBlocked = dialog.acceptCheck.blockSignals(True)
        dialog.acceptCheck.setChecked(True)
        dialog.acceptCheck.blockSignals(wasBlocked)
        controller = dialog.controller
        saved = (controller.state, controller.request, controller.errorEvent, controller.job)
        self.addCleanup(lambda: (setattr(controller, "state", saved[0]), setattr(controller, "request", saved[1]), setattr(controller, "errorEvent", saved[2]),
                                 setattr(controller, "job", saved[3])))
        controller.job = None
        controller.state = "idle"
        controller.errorEvent = None
        patcher = mock.patch.object(manager_catalog, "termsAccepted", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        filePath = os.path.join(self.tmp, "a file.txt")
        with open(filePath, "w") as handle:
            handle.write("x")
        for text in (filePath, os.path.join(filePath, "sub")):
            dialog.destEdit.setText(text)
            dialog.updateState()
            self.assertFalse(dialog.downloadButton.enabled, text)
            self.assertIn("is a file, not a folder", dialog.hintLabel.text, text)
        # The controller refuses it too (Resume of a saved request)
        with mock.patch.object(controller, "entry", return_value=entry), mock.patch.object(manager_catalog, "fetchEnabled", return_value=True), \
                mock.patch.object(manager_catalog.jobrunner, "instance") as runner:
            self.assertIsNone(controller.start({"entry": entry["id"], "dest": filePath, "termsSha": "a" * 64}))
            self.assertIn("is a file, not a folder", controller.lastRequestError or "")
            runner.assert_not_called()
        # A folder (existing or new) is still accepted
        for text in (self.tmp, os.path.join(self.tmp, "new folder")):
            dialog.destEdit.setText(text)
            dialog.updateState()
            self.assertNotIn("is a file", dialog.hintLabel.text, text)


if __name__ == "__main__":
    unittest.main()

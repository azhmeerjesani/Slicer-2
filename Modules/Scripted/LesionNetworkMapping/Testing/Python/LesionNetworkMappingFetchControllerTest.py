"""Tests of the Get a connectome… controller (``manager_catalog.FetchController``) after the application ended during a fetch.

The application starts the fetch through the PythonSlicer launcher, which outlives a crashed application, so before this
was fixed the job kept downloading unseen; the bundle it finished was never registered, and Resume failed on its lock.
Now the job gets ``--parent-pid``, a bundle that a job finished unseen is registered on the next launch, and a fetch that
another live process runs is not offered for resuming. No network is used.

It needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingFetchControllerTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import collections
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

import numpy as np

ENTRY_ID = "fake-gsp"


class LesionNetworkMappingFetchControllerTest(unittest.TestCase):
    def setUp(self):
        from LesionNetworkMapping import CONNECTOMES_SETTING
        from LesionNetworkMappingLib import catalog
        from LesionNetworkMappingUI import manager_catalog

        self.mc = manager_catalog
        self.tmp = tempfile.mkdtemp(prefix="lnm fetch controller ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        data = copy.deepcopy(catalog.load_catalog().data)
        entry = copy.deepcopy(catalog.load_catalog().get("gsp1000-ilxiks"))
        entry["id"] = ENTRY_ID
        data["entries"] = [entry]
        self.entry = entry
        self.catalogPath = os.path.join(self.tmp, "catalog.json")
        with open(self.catalogPath, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.dest = os.path.join(self.tmp, "dest")
        os.makedirs(self.dest)
        self.request = {"entry": ENTRY_ID, "dest": self.dest, "subset": None, "keepArchives": False, "streams": 2, "termsSha": "a" * 64}
        settings = qt.QSettings()
        for key in (manager_catalog.ACTIVE_FETCH_SETTING, CONNECTOMES_SETTING):
            saved = settings.value(key)
            self.addCleanup(self._restore, key, saved)
        settings.setValue(manager_catalog.ACTIVE_FETCH_SETTING, json.dumps(self.request))
        self.controller = manager_catalog.FetchController()
        self.controller.catalogPath = self.catalogPath

    @staticmethod
    def _restore(key, value):
        if value is None:
            qt.QSettings().remove(key)
        else:
            qt.QSettings().setValue(key, value)

    def _state(self, complete):
        from LesionNetworkMappingLib import fetchconnectome, jobs

        work = fetchconnectome.work_dir(self.entry, self.dest)
        os.makedirs(work, exist_ok=True)
        sizes = {"a.tar": 100, "b.tar": 100}
        archives = {"a.tar": {"status": "done", "bytes": 100}, "b.tar": {"status": "done" if complete else "partial", "bytes": 100 if complete else 40}}
        jobs.write_json_atomic(os.path.join(work, "state.json"), {"format": fetchconnectome.STATE_FORMAT, "version": 1, "entry": ENTRY_ID, "sizes": sizes, "archives": archives, "files": {},
                                                                  "complete": complete})
        if not complete:  # an interrupted fetch leaves its subjects so far in the bundle (a state whose bundle is gone is not resumed)
            bundle = fetchconnectome.bundle_path(self.entry, self.dest)
            os.makedirs(bundle, exist_ok=True)
            jobs.write_json_atomic(os.path.join(bundle, "connectome.partial.json"), {"format": "lnmc", "subjects": []})

    def _bundle(self):
        from LesionNetworkMappingLib import connectome, fetchconnectome

        path = fetchconnectome.bundle_path(self.entry, self.dest)
        mask = np.zeros((4, 5, 6), dtype=bool)
        mask[1:3, 1:4, 1:5] = True
        writer = connectome.ConnectomeWriter(path, mask, np.diag([2.0, 2.0, 2.0, 1.0]), name="fetched", dtype="float32")
        rng = np.random.default_rng(3)
        for index in range(3):
            writer.add_subject(f"s{index:02d}", [rng.standard_normal((int(mask.sum()), 12))])
        return writer.finalize()

    def _liveProcess(self):
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def _lock(self, pid):
        from LesionNetworkMappingLib import fetchconnectome, jobs

        path = fetchconnectome.lock_path(self.entry, self.dest)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "host": jobs._host(), "token": "t", "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f)
        return path

    def test_a_failed_job_explains_itself_without_command_line_flags(self):
        from LesionNetworkMappingLib import jobs

        controller = self.controller
        controller.request = dict(self.request)
        cli = "x.tar: the MD5 is wrong. Run again with --accept-checksum-mismatch to keep them, or delete the folder."
        job = jobs.JobError(cli, code="checksum", override="--accept-checksum-mismatch", file="x.tar")
        fake = type("FakeJob", (), {"succeeded": lambda self: False, "state": "failed", "failureReason": cli,
                                    "errorEvent": {"code": "checksum", "file": "x.tar", **job.fields}, "lastEvent": {}, "progress": None, "eta": None, "warnings": []})()
        controller._onDone(fake)
        status = controller.status()
        self.assertTrue(status["mismatch"])
        self.assertFalse(status["termsChanged"])
        self.assertNotIn("--", status["error"])
        self.assertIn("x.tar", status["error"])
        self.assertIn("Keep this data anyway", status["error"])
        fake.errorEvent = {"code": "terms_mismatch"}
        controller._onDone(fake)
        status = controller.status()
        self.assertTrue(status["termsChanged"])
        self.assertFalse(status["mismatch"])
        self.assertFalse(status["damaged"])
        fake.errorEvent = {"code": "damaged_member", "file": "x.tar"}
        controller._onDone(fake)
        status = controller.status()
        self.assertTrue(status["damaged"])
        self.assertFalse(status["mismatch"])

    def test_the_fetch_job_stops_with_the_application(self):
        args = self.controller.arguments(self.request)
        self.assertIn("--parent-pid", args)
        self.assertEqual(args[args.index("--parent-pid") + 1], str(os.getpid()))
        resumed = self.controller.arguments(self.request, resume=True)
        self.assertEqual(resumed[resumed.index("--parent-pid") + 1], str(os.getpid()))

    def test_a_bundle_finished_after_the_application_ended_is_registered(self):
        from LesionNetworkMapping import LesionNetworkMappingLogic

        path = self._bundle()
        self._state(complete=True)
        self.assertIsNone(self.controller.unfinished())  # nothing to resume
        self.assertIn(os.path.abspath(path), [os.path.abspath(p) for p in LesionNetworkMappingLogic.registeredConnectomes()])
        self.assertIsNone(qt.QSettings().value(self.mc.ACTIVE_FETCH_SETTING))
        self.assertEqual(self.controller.state, "done")
        self.assertEqual(self.controller.status()["path"], path)

    def test_an_unfinished_bundle_is_offered_and_not_registered(self):
        self._state(complete=False)
        found = self.controller.unfinished()
        self.assertIsNotNone(found)
        self.assertEqual(found[0], self.request)
        self.assertEqual(self.controller.state, "idle")
        self.assertEqual(json.loads(qt.QSettings().value(self.mc.ACTIVE_FETCH_SETTING)), self.request)

    def test_an_unfinished_download_can_be_discarded_and_is_not_offered_again(self):
        self._state(complete=False)
        self.assertIsNotNone(self.controller.unfinished())
        self.assertEqual(self.controller.discard(), self.request)
        self.assertIsNone(qt.QSettings().value(self.mc.ACTIVE_FETCH_SETTING))
        self.assertIsNone(self.controller.savedRequest())
        self.assertIsNone(self.controller.unfinished())
        self.assertIsNone(self.controller.resume())  # nothing to resume any more
        self.assertEqual(self.controller.state, "idle")
        self.assertTrue(os.path.isdir(self.dest))  # the files stay: the notice tells the user where
        self.assertIn(self.dest, self.controller.discardedNotice(self.request))
        self.assertIsNone(self.controller.discard())  # nothing left to forget

    def test_a_paused_download_can_be_discarded(self):
        self._state(complete=False)
        self.controller.request = dict(self.request)
        self.controller.state = "paused"
        self.assertIsNotNone(self.controller.unfinished())
        self.assertEqual(self.controller.discard(), self.request)
        self.assertIsNone(self.controller.request)
        self.assertEqual(self.controller.state, "idle")
        self.assertIsNone(self.controller.unfinished())
        self.assertIsNone(qt.QSettings().value(self.mc.ACTIVE_FETCH_SETTING))

    def test_the_catalog_dialog_has_a_discard_for_the_unfinished_download(self):
        from LesionNetworkMappingUI import catalog_dialog

        self._state(complete=False)
        dialog = catalog_dialog.CatalogDialog(catalogPath=self.catalogPath)
        self.addCleanup(dialog.deleteLater)
        dialog.controller = self.controller
        self.controller.observers.append(dialog.onControllerChanged)
        dialog.updateResumeBanner()
        self.assertFalse(dialog.resumeBanner.isHidden())
        labels = [button.text for button in dialog.resumeBanner.findChildren("QToolButton")]
        self.assertEqual(labels, ["Resume", "Discard"])
        dialog.onDiscard()
        self.assertTrue(dialog.resumeBanner.isHidden())
        self.assertIsNone(qt.QSettings().value(self.mc.ACTIVE_FETCH_SETTING))
        self.assertIn(self.dest, dialog.notice.findChild("QLabel", "LNMBannerText").text.replace("\u200b", ""))
        # a paused download has the button in its progress card too
        self.controller.request = dict(self.request)
        self.controller.state = "paused"
        dialog.onControllerChanged()
        self.assertFalse(dialog.discardButton.isHidden())
        self.controller.state = "idle"
        dialog.onControllerChanged()
        self.assertTrue(dialog.progressCard.isHidden())  # the card (and its Discard) goes with the state

    def test_a_fetch_another_live_process_runs_is_reported_not_offered(self):
        from LesionNetworkMappingLib import fetchconnectome

        self._state(complete=False)
        process = self._liveProcess()
        self._lock(process.pid)
        holder = self.controller.runningElsewhere()
        self.assertIsNotNone(holder)
        self.assertEqual(holder["pid"], process.pid)
        self.assertIsNone(self.controller.unfinished())  # Resume would fail on the lock
        self.assertEqual(json.loads(qt.QSettings().value(self.mc.ACTIVE_FETCH_SETTING)), self.request)  # kept until that job ends
        process.kill()
        process.wait()
        self.assertIsNone(fetchconnectome.running_fetch(self.entry, self.dest))  # the holder is gone: its lock is stale
        self.assertIsNone(self.controller.runningElsewhere())
        self.assertIsNotNone(self.controller.unfinished())

    def test_a_second_window_does_not_offer_or_discard_a_download_another_window_runs(self):
        from LesionNetworkMappingUI import catalog_dialog

        self._state(complete=False)
        process = self._liveProcess()
        self._lock(process.pid)
        usage = shutil.disk_usage
        shutil.disk_usage = lambda path: collections.namedtuple("usage", "total used free")(10**13, 0, 10**13)
        self.addCleanup(setattr, shutil, "disk_usage", usage)
        dialog = catalog_dialog.CatalogDialog(catalogPath=self.catalogPath)
        self.addCleanup(dialog.deleteLater)
        dialog.controller = self.controller
        dialog.entry = self.entry
        dialog.destEdit.setText(self.dest)
        dialog.currentTerms = lambda: {"sha": "a" * 64}
        dialog.acceptCheck.setChecked(True)
        self.mc.termsAccepted = lambda sha: True
        dialog.updateState()
        self.assertFalse(dialog.downloadButton.enabled)
        self.assertIn("another Slicer window", dialog.hintLabel.text)
        dialog.onDiscard()  # must not clear the other window's saved request
        self.assertEqual(json.loads(qt.QSettings().value(self.mc.ACTIVE_FETCH_SETTING)), self.request)
        self.assertIsNone(self.controller.discard())
        self.assertIsNotNone(qt.QSettings().value(self.mc.ACTIVE_FETCH_SETTING))
        process.kill()
        process.wait()
        dialog.updateState()
        self.assertTrue(dialog.downloadButton.enabled)  # the holder is gone: Resume is offered again


if __name__ == "__main__":
    unittest.main()

"""Tests of the Get a connectome… controller (``manager_catalog.FetchController``) after the application ended during a fetch.

The application starts the fetch through the PythonSlicer launcher, which outlives a crashed application, so before this
was fixed the job kept downloading unseen; the bundle it finished was never registered, and Resume failed on its lock.
Now the job gets ``--parent-pid``, a bundle that a job finished unseen is registered on the next launch, and a fetch that
another live process runs is not offered for resuming. No network is used.

It needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingFetchControllerTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

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


if __name__ == "__main__":
    unittest.main()

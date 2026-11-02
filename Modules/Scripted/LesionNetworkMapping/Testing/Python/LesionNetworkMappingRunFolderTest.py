"""Tests of the run folder of a Map network run (``LesionNetworkMappingLogic.runDirectory``).

Two windows or processes that press Map network in the same second for lesions with the same name (the default
"Lesions-Lesion") must get two run folders: the folder is reserved exclusively when it is named, so one run never
writes its lesion and maps into the other's folder (bug bash BB-1, same-second-map-runs-share-run-folder).

They need the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRunFolderTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

STAMP = "20261102-013344"


class _StudyContext:
    """The part of the Map context that ``runDirectory`` reads: a study and its run root."""

    study = object()

    def __init__(self, root):
        self.root = root

    def runRoot(self):
        return self.root


class LesionNetworkMappingRunFolderTest(unittest.TestCase):
    def setUp(self):
        import LesionNetworkMapping
        from LesionNetworkMappingUI import jobrunner

        self.module = LesionNetworkMapping
        self.logic = LesionNetworkMapping.LesionNetworkMappingLogic
        self.jobrunner = jobrunner
        self.tmp = tempfile.mkdtemp(prefix="lnm run folder ")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sameSecond(self):
        """Every call of ``runDirectory`` sees the same wall-clock second."""
        return mock.patch.object(self.module.time, "strftime", return_value=STAMP)

    def test_same_second_same_lesion_gets_two_folders(self):
        ctx = _StudyContext(os.path.join(self.tmp, "study", "derivatives", "lnm"))
        with self.sameSecond():
            first = self.logic.runDirectory(ctx, "Lesions-Lesion")
            second = self.logic.runDirectory(ctx, "Lesions-Lesion")
        runs = os.path.join(self.tmp, "study", "derivatives", "lnm", "runs")
        self.assertEqual(first, os.path.join(runs, f"{STAMP}_Lesions-Lesion"))
        self.assertEqual(second, os.path.join(runs, f"{STAMP}-2_Lesions-Lesion"))
        # Reserved when named: a window that asks next (in this or another process) sees the folder as taken
        self.assertTrue(os.path.isdir(first))
        self.assertTrue(os.path.isdir(second))

    def test_default_run_root_is_reserved_too(self):
        root = os.path.join(self.tmp, "cache", "LNM", "runs")
        with self.sameSecond(), mock.patch.object(self.jobrunner, "defaultRunRoot", return_value=root):
            paths = [self.logic.runDirectory(None, "Lesions-Lesion") for _ in range(3)]
        self.assertEqual([os.path.basename(p) for p in paths], [f"{STAMP}_Lesions-Lesion", f"{STAMP}-2_Lesions-Lesion", f"{STAMP}-3_Lesions-Lesion"])
        self.assertTrue(all(os.path.isdir(p) for p in paths))

    def test_concurrent_callers_never_share_a_folder(self):
        ctx = _StudyContext(os.path.join(self.tmp, "shared"))
        count = 8
        barrier = threading.Barrier(count)
        paths, errors = [], []

        def ask():
            try:
                barrier.wait(10)
                paths.append(self.logic.runDirectory(ctx, "Lesions-Lesion"))
            except Exception as exc:  # reported below
                errors.append(exc)

        with self.sameSecond():
            threads = [threading.Thread(target=ask) for _ in range(count)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len(paths), count)
        self.assertEqual(len(set(paths)), count, paths)

    def test_unwritable_root_returns_the_path_unreserved(self):
        # A run root below a file cannot be created: the name is still returned and the step that writes reports the error
        blocker = os.path.join(self.tmp, "file")
        with open(blocker, "w") as f:
            f.write("x")
        ctx = _StudyContext(os.path.join(blocker, "lnm"))
        with self.sameSecond():
            path = self.logic.runDirectory(ctx, "Lesions-Lesion")
        self.assertEqual(path, os.path.join(blocker, "lnm", "runs", f"{STAMP}_Lesions-Lesion"))
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()

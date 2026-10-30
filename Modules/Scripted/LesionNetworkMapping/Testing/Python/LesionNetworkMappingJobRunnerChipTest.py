"""The status chip and the log of a job that finished with a partial result (some of its items done, some failed).

A batch where one subject failed ended with exit code 1 and no error event, so the chip read "Run failed: exit code 1"
although the other subjects were normalized. The job now reports an error event with ``partial`` and the runner shows its
message ("1 of 2 normalized, 1 failed: ...") instead of "Run failed: ...". It needs the Slicer application (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingJobRunnerChipTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import logging
import os
import shutil
import tempfile
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

FAKE_JOB = """
import json
import sys

for event in EVENTS:
    print(json.dumps(event), flush=True)
sys.exit(1)
"""


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


class LesionNetworkMappingJobRunnerChipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm chip ")
        self.chips = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_job(self, name, events):
        from LesionNetworkMappingUI import jobrunner

        with open(os.path.join(self.tmp, f"{name}.py"), "w", encoding="utf-8") as f:
            f.write(f"EVENTS = {events!r}\n" + FAKE_JOB)
        runner = jobrunner.instance()
        original = runner._setChip
        runner._setChip = lambda job, text, level: (self.chips.append((text, level)), original(job, text, level))[1]
        capture = _Capture()
        logging.getLogger().addHandler(capture)
        try:
            job = runner.submit(jobrunner.JobSpec(name, [], title="Normalizing", heavy=False, kind="normalization", runRoot=self.tmp, env={"PYTHONPATH": self.tmp}, name=name))
            self.assertTrue(job.waitForFinished(60), job.state)
        finally:
            logging.getLogger().removeHandler(capture)
            runner._setChip = original
            runner.clearStatus()
        return job, capture.messages

    def test_partial_result_reads_like_the_toast(self):
        message = "1 of 2 normalized, 1 failed: The scan file does not exist"
        job, logs = self.run_job("partial_job", [{"event": "start"}, {"event": "error", "code": "subjects_failed", "message": message, "partial": True}, {"event": "done"}])
        self.assertEqual(job.state, "failed")
        self.assertEqual(job.exitCode, 1)
        self.assertTrue(job.isPartial())
        self.assertEqual(job.failureReason, message)
        self.assertEqual(self.chips[-1][1], "warn")
        self.assertTrue(self.chips[-1][0].startswith("1 of 2 normalized, 1 failed"), self.chips)
        self.assertFalse(any("Run failed" in (text or "") or "exit code" in (text or "") for text, _ in self.chips), self.chips)
        self.assertTrue(any("finished with failures" in text and message in text for text in logs), logs)
        self.assertFalse(any("exit code 1" in text for text in logs), logs)

    def test_total_failure_still_reads_run_failed(self):
        message = "No subject was normalized (2 failed): The scan file does not exist"
        job, logs = self.run_job("total_job", [{"event": "start"}, {"event": "error", "code": "subjects_failed", "message": message, "partial": False}, {"event": "done"}])
        self.assertEqual(job.state, "failed")
        self.assertFalse(job.isPartial())
        self.assertEqual(self.chips[-1], (f"Run failed: {message[:59]}…", "warn"))
        self.assertTrue(any("failed" in text and "finished with failures" not in text for text in logs), logs)


if __name__ == "__main__":
    unittest.main()

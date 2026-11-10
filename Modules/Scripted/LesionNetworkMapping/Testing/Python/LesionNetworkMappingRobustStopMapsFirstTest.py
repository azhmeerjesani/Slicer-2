"""Stop during the Robust card's 'maps the lesions first' phase drops the pending analysis (BB-1).

A stopped maps job can still end 'done' (it was past its last cancel check). The card must not start the analysis
that was waiting for the maps then, and must say the run was stopped. It needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRobustStopMapsFirstTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class _Job:
    def __init__(self, state, stopRequested):
        self.state = state
        self.stopRequested = stopRequested

    def succeeded(self):
        return self.state == "done"


class _Card:
    """The few members of RobustCard that _onJobFinished uses."""

    def __init__(self, job):
        self._cleanedUp = False
        self.mapsJob = job
        self.pendingKind = "sweep"
        self.notice = ""
        self.started = []

    def _updateProgress(self, force=False):
        pass

    def currentMapsTsv(self):
        return "maps.tsv"

    def runState(self, kind):
        return True, ""

    def start(self, kind):
        self.started.append(kind)

    def setNotice(self, text):
        self.notice = text

    def refresh(self):
        pass


class LesionNetworkMappingRobustStopMapsFirstTest(unittest.TestCase):
    def _finish(self, state, stopRequested):
        from LesionNetworkMappingUI import card_robust

        job = _Job(state, stopRequested)
        card = _Card(job)
        card_robust.RobustCard._onJobFinished(card, job)
        return card

    def test_stoppedMapsJobThatEndsDoneDropsTheAnalysis(self):
        card = self._finish("done", True)
        self.assertEqual(card.started, [])
        self.assertIsNone(card.pendingKind)
        self.assertIn("stopped", card.notice)

    def test_cancelledMapsJobSaysStopped(self):
        card = self._finish("cancelled", True)
        self.assertEqual(card.started, [])
        self.assertIn("stopped", card.notice)

    def test_unstoppedMapsJobStartsTheAnalysis(self):
        card = self._finish("done", False)
        self.assertEqual(card.started, ["sweep"])
        self.assertEqual(card.notice, "")

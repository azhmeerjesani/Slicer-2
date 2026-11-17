"""Get atlases can be stopped, and gives up at once when there is no connection.

The atlas manager passed no cancel token to the fetch layer, so a download could not be stopped, and offline each of the
5 atlases retried (1 + 3 + 8 s) before the failure showed, about a minute in all. Now ``AtlasManager.stop()`` cancels the
running download, and after the first atlas that fails for lack of a connection the others are not tried.

Nothing is downloaded: the fetch layer is replaced by stand-ins.

They need the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingAtlasStopUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import time
import unittest
from unittest import mock

try:
    import qt  # noqa: F401
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

IDS = ["schaefer2018-400-7n-2mm-2009c", "schaefer2018-200-7n-2mm-2009c", "schaefer2018-100-7n-2mm-2009c"]


def wait(condition, seconds=30.0):
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.02)
    return condition()


class LesionNetworkMappingAtlasStopUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()

    def test_offline_stops_after_the_first_atlas(self):
        from LesionNetworkMappingLib import fetch
        from LesionNetworkMappingUI import card_clusters

        calls = []

        def offline(resource, cache=None, **kwargs):
            calls.append(resource.id)
            raise fetch.OfflineError("No connection to the server.", resource_id=resource.id)

        manager = card_clusters.AtlasManager()
        with mock.patch.object(card_clusters.AtlasManager, "isCached", return_value=False), mock.patch.object(fetch, "fetch_set", offline):
            self.assertTrue(manager.fetch(IDS))
            self.assertTrue(wait(lambda: manager.fetching is None))
        self.assertEqual(len(calls), 1, "every atlas retried the missing connection")
        self.assertTrue(manager.message.startswith("The atlases could not be downloaded."), manager.message)

    def test_stop_cancels_the_running_download(self):
        from LesionNetworkMappingLib import fetch, jobs
        from LesionNetworkMappingUI import card_clusters

        started = []

        def slow(resource, cache=None, cancel=None, **kwargs):
            started.append(resource.id)
            deadline = time.time() + 20
            while time.time() < deadline:
                if cancel is not None and cancel.cancelled():
                    raise jobs.Cancelled()
                time.sleep(0.02)

        manager = card_clusters.AtlasManager()
        with mock.patch.object(card_clusters.AtlasManager, "isCached", return_value=False), mock.patch.object(fetch, "fetch_set", slow):
            self.assertFalse(manager.stop())  # nothing runs
            self.assertTrue(manager.fetch(IDS))
            self.assertTrue(wait(lambda: bool(started), 5))
            self.assertTrue(manager.stop())
            self.assertTrue(wait(lambda: manager.fetching is None, 5), "Stop did not end the download")
        self.assertEqual(len(started), 1, "the other atlases went on after Stop")
        self.assertEqual(manager.message, "The download was stopped.")
        self.assertFalse(manager.busy())
        self.assertTrue(manager.stopped, "a stop must be told apart from a failure so that only the Clusters card words it")
        with mock.patch.object(card_clusters.AtlasManager, "isCached", return_value=False), mock.patch.object(fetch, "fetch_set", slow):
            self.assertTrue(manager.fetch(IDS))
            self.assertFalse(manager.stopped, "a new download clears the stop")
            manager.stop()
            self.assertTrue(wait(lambda: manager.fetching is None, 5))


if __name__ == "__main__":
    unittest.main()

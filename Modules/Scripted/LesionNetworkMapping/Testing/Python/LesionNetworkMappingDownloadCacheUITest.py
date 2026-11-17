"""One download cache for every card: a file downloaded by one card serves every card that uses it.

With ``SLICER_LNM_CACHE`` unset, Clusters' Get atlases, Circuits' Get GM prior and Run everything's atlas download used
to write to the library's default folder (``~/.cache/SlicerStudio/LNM``), while the Result card's peak regions, Draw's
lesion checks, the Cohort card's tissue priors and Tutorials read ``<application cache>/LNM``: the atlases that Clusters
had just fetched never named the peaks. Every caller in the application now resolves the folder with
``fetch.app_cache_dir()`` (``$SLICER_LNM_CACHE``, else ``<application cache>/LNM``), and the jobs the application starts
get that folder too.

Nothing is downloaded: the fetch layer is replaced by recorders of the folder each caller asks for.

They need the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingDownloadCacheUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import time
import types
import unittest
from unittest import mock

try:
    import qt  # noqa: F401
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

ATLAS_ID = "schaefer2018-400-7n-2mm-2009c"


def wait(condition, seconds=30.0):
    deadline = time.time() + seconds
    while not condition() and time.time() < deadline:
        slicer.app.processEvents()
        time.sleep(0.02)
    return condition()


class LesionNetworkMappingDownloadCacheUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()

    def setUp(self):
        from LesionNetworkMappingLib import fetch

        self.fetch = fetch
        environment = mock.patch.dict(os.environ)  # restored after the test, SLICER_LNM_CACHE included
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(fetch.CACHE_ENV, None)
        self.expected = fetch.cache_dir(os.path.join(slicer.app.cachePath, "LNM"))
        self.seen = []

    def recorder(self, result=None):
        """A stand-in of a fetch function that records the cache folder it was asked for."""
        fetch = self.fetch

        def record(resource, cache=None, *args, **kwargs):
            self.seen.append(fetch.cache_dir(cache))
            return result

        return record

    def assertAllExpected(self, what):
        self.assertTrue(self.seen, f"{what}: the fetch layer was not called")
        self.assertEqual(set(self.seen), {self.expected}, f"{what} uses another download cache than the application's")
        self.seen.clear()

    def test_downloads_land_where_the_readers_look(self):
        fetch = self.fetch
        from LesionNetworkMappingUI import card_circuits, card_clusters, card_pipeline, jobrunner, manager_fastindex

        # Writers: Clusters' Get atlases (and Networks', Fast index's: the same shared manager), Run everything's atlas,
        # Circuits' Get GM prior
        manager = card_clusters.AtlasManager()
        with mock.patch.object(card_clusters.AtlasManager, "isCached", return_value=False), mock.patch.object(fetch, "fetch_set", self.recorder()):
            self.assertTrue(manager.fetch([ATLAS_ID]))
            self.assertTrue(wait(lambda: manager.fetching is None))
        self.assertAllExpected("Clusters' Get atlases")

        page = types.SimpleNamespace(atlasJob=None, atlasStatus=mock.Mock(), _update=lambda: None, _atlasCached=lambda refresh=False: None)
        with mock.patch.object(fetch, "fetch_set", self.recorder()):
            card_pipeline.OptionsDialog.downloadAtlas(page)
            self.assertTrue(wait(lambda: page.atlasJob is None))
        self.assertAllExpected("Run everything's atlas Download")

        circuits = types.SimpleNamespace(gmFetch=None, circuitSpace=lambda: (None, "2009"), gmTimer=mock.Mock(), _refreshGm=lambda: None)
        with mock.patch.object(fetch, "fetch", self.recorder()):
            self.assertTrue(card_circuits.CircuitsCard.getGmPrior(circuits, confirm=False))
            circuits.gmFetch["thread"].join(10)
        self.assertAllExpected("Circuits' Get GM prior")

        # Readers of the cards: the atlas state of Clusters, Run everything's atlas, Fast index, the GM prior of Circuits
        with mock.patch.object(fetch, "is_cached", self.recorder(False)):
            card_clusters.AtlasManager().isCached(ATLAS_ID, refresh=True)
            manager_fastindex.missingAtlases([ATLAS_ID])
        self.assertAllExpected("the atlas checks of Clusters and Fast index")
        with mock.patch.object(fetch, "cached_path", self.recorder()):
            card_pipeline._atlasPaths("schaefer400", refresh=True)
            card_circuits.CircuitsCard.gmPrior(types.SimpleNamespace(gmFile=None, circuitSpace=lambda: (None, "2009")))
        self.assertAllExpected("the cached atlas of Run everything and the GM prior of Circuits")

        # The Result card's peak regions (the lookup that stayed empty after Get atlases)
        widget = slicer.modules.LesionNetworkMappingWidget
        from LesionNetworkMappingLib import atlases

        looked = []
        with mock.patch.object(atlases, "peak_regions", lambda points, space=None, cache=None, atlases=None: looked.append(fetch.cache_dir(cache))), \
                mock.patch.object(widget, "_showPeakRegions", lambda peaks, regions, unknownSpace=False: None):
            # A map on a standard grid (a map whose space cannot be told is not looked up at all)
            from LesionNetworkMappingLib import geometry

            widget._lookupPeakRegions([{"world": (0.0, 0.0, 0.0)}], geometry.GRID_BY_ID["NLin6-2mm"].grid)
            self.assertTrue(wait(lambda: getattr(widget, "_peakRegionJob", None) is None))
        self.assertEqual(looked, [self.expected])

        # Jobs started by the application (the pipeline's cached atlases) get the same folder
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.fetch", [])
        self.assertEqual(fetch.cache_dir(jobrunner.JobRunner.environment(None, spec, 1).get(fetch.CACHE_ENV)), self.expected)

    def test_failed_downloads_show_a_sentence_without_the_exception_class(self):
        """Run everything's atlas Download, Circuits' Get GM prior and the Cohort card's tissue priors name the problem in words."""
        fetch = self.fetch
        from LesionNetworkMappingUI import card_circuits, card_pipeline

        sentence = "No connection to templateflow.s3.amazonaws.com: the proxy server cannot be reached. Check the network and try again."

        def failing(resource, cache=None, *args, **kwargs):
            raise fetch.OfflineError(sentence)

        def other(resource, cache=None, *args, **kwargs):
            raise ValueError("HTTPSConnectionPool(host='a.example', port=443): Max retries exceeded")

        def assertPlain(text, what):
            for raw in ("OfflineError", "ValueError", "FetchError", "HTTPSConnectionPool", "Max retries"):
                self.assertNotIn(raw, text, what)

        # Run everything: the text of the atlas status
        for function, expected in ((failing, sentence), (other, None)):
            shown = []
            page = types.SimpleNamespace(atlasJob=None, atlasStatus=types.SimpleNamespace(setText=shown.append), _update=lambda: None, _atlasCached=lambda refresh=False: None)
            with mock.patch.object(fetch, "fetch_set", function):
                card_pipeline.OptionsDialog.downloadAtlas(page)
                self.assertTrue(wait(lambda shown=shown: any(text.startswith("The download failed") for text in shown)), shown)
            text = next(t for t in shown if t.startswith("The download failed"))
            assertPlain(text, "Run everything's atlas Download")
            if expected:
                self.assertEqual(text, f"The download failed: {expected}")

        # Circuits: the notice
        for function, expected in ((failing, sentence), (other, None)):
            notices = []
            circuits = types.SimpleNamespace(
                gmFetch=None, circuitSpace=lambda: (None, "2009"), gmTimer=mock.Mock(), _refreshGm=lambda: None,
                _setNotice=notices.append, _refreshTargetsState=lambda: None, _cleanedUp=False,
            )
            with mock.patch.object(fetch, "fetch", function):
                self.assertTrue(card_circuits.CircuitsCard.getGmPrior(circuits, confirm=False))
                circuits.gmFetch["thread"].join(10)
            card_circuits.CircuitsCard._pollGmFetch(circuits)
            self.assertEqual(len(notices), 1, notices)
            assertPlain(notices[0], "Circuits' Get GM prior")
            if expected:
                self.assertEqual(notices[0], f"The GM prior could not be downloaded: {expected}")

    def test_cohort_priors_failure_is_a_sentence(self):
        try:
            from StudioLib import libcard_cohort
        except ImportError as exc:
            self.skipTest(f"Studio is not part of this build ({exc})")
        fetch = self.fetch
        for exception in (fetch.OfflineError("No connection to a.example. Check the network and try again."), ValueError("HTTPSConnectionPool: Max retries exceeded")):
            worker = libcard_cohort._Worker(lambda result, error: None)
            with mock.patch.object(fetch, "fetch", side_effect=exception):
                worker._work(libcard_cohort.fetchPriorsWork, {"cache": os.path.join(slicer.app.temporaryPath, "lnm-priors-failure")})
            (result, error), = [worker._queue.get_nowait()]
            self.assertIsNone(result)
            for raw in ("OfflineError", "ValueError", "FetchError", "HTTPSConnectionPool", "Max retries"):
                self.assertNotIn(raw, error)
            if isinstance(exception, fetch.OfflineError):
                self.assertEqual(error, str(exception))
        # Another job keeps its class name (the diagnostics are not downloads)

        def broken(progress, cancel):
            raise RuntimeError("boom")

        worker = libcard_cohort._Worker(lambda result, error: None)
        worker._work(broken, {})
        self.assertEqual(worker._queue.get_nowait(), (None, "RuntimeError: boom"))

    def test_studio_readers_use_the_same_folder(self):
        try:
            from StudioLib import lesionqc, libcard_cohort, libcard_tutorials
        except ImportError as exc:
            self.skipTest(f"Studio is not part of this build ({exc})")
        fetch = self.fetch
        self.assertEqual(fetch.cache_dir(libcard_cohort.cacheFolder()), self.expected)  # the Cohort card's tissue priors
        self.assertEqual(fetch.cache_dir(libcard_tutorials.cacheFolder()), self.expected)  # Tutorials
        self.assertEqual(fetch.cache_dir(lesionqc.QcCore.cacheDir(None)), self.expected)  # Draw's "Where" chip and brain masks

    def test_environment_variable_wins_everywhere(self):
        fetch = self.fetch
        from LesionNetworkMappingUI import card_clusters, jobrunner

        folder = os.path.join(slicer.app.temporaryPath, "lnm-download-cache-env")
        os.environ[fetch.CACHE_ENV] = folder
        self.expected = fetch.cache_dir(folder)
        self.assertEqual(fetch.app_cache_dir(), self.expected)
        with mock.patch.object(fetch, "is_cached", self.recorder(False)):
            card_clusters.AtlasManager().isCached(ATLAS_ID, refresh=True)
        self.assertAllExpected("Clusters with SLICER_LNM_CACHE")
        spec = jobrunner.JobSpec("LesionNetworkMappingLib.fetch", [])
        self.assertEqual(fetch.cache_dir(jobrunner.JobRunner.environment(None, spec, 1).get(fetch.CACHE_ENV)), self.expected)
        try:
            from StudioLib import libcard_cohort
        except ImportError:
            return
        self.assertEqual(fetch.cache_dir(libcard_cohort.cacheFolder()), self.expected)


if __name__ == "__main__":
    unittest.main()

"""A stopped reference-map download says so in a sentence, not with the internal state word "cancelled".

Runs under plain Python or the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRefMapStoppedNoticeTest
"""

import types
import unittest

try:
    import slicer  # noqa: F401
    from LesionNetworkMappingUI import card_compare
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc


class LesionNetworkMappingRefMapStoppedNoticeTest(unittest.TestCase):
    def _notice(self, **jobFields):
        notices = []
        fake = types.SimpleNamespace(_cleanedUp=False, _setNotice=notices.append, updateState=lambda: None)
        cls = next(c for c in vars(card_compare).values() if isinstance(c, type) and "_onAdditionDone" in vars(c))
        cls._onAdditionDone(fake, types.SimpleNamespace(**{"results": [], "failureReason": None, **jobFields}))
        return notices[-1]

    def test_stopped_job_has_a_sentence(self):
        text = self._notice(state="cancelled")
        self.assertNotIn("cancelled", text)
        self.assertIn("stopped", text)

    def test_failed_job_keeps_its_reason(self):
        text = self._notice(state="failed", failureReason="No connection")
        self.assertIn("No connection", text)


if __name__ == "__main__":
    unittest.main()

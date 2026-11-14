"""The "Get atlases" confirmation is a Studio dialog (VISUAL 4.7) and says "1 atlas file" for one atlas.

It was a plain QMessageBox (the legacy question bubble and coloured OK/Cancel icons) that read "Download 1 atlas files".

They need the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingGetAtlasesDialogUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest
from unittest import mock

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class _Manager:
    def missing(self, ids):
        return list(ids)

    def sizeText(self, ids):
        return "about 0.1 MB"

    def describe(self, ids):
        return [f"Atlas {i} (CC0)" for i in ids]


class LesionNetworkMappingGetAtlasesDialogUITest(unittest.TestCase):
    def test_singular_and_plural_text(self):
        from LesionNetworkMappingUI import card_clusters

        one = card_clusters.fetchQuestionText(_Manager(), ["a"])
        self.assertTrue(one.startswith("Download 1 atlas file (about 0.1 MB)"), one)
        two = card_clusters.fetchQuestionText(_Manager(), ["a", "b"])
        self.assertTrue(two.startswith("Download 2 atlas files (about 0.1 MB)"), two)

    def test_dialog_has_studio_chrome_and_answers(self):
        from LesionNetworkMappingUI import card_clusters

        seen = {}
        for button, expected in (("LNMGetAtlasesDownloadButton", True), ("LNMGetAtlasesCancelButton", False)):

            def run(self, button=button):
                seen["dialog"] = self
                seen["chrome"] = bool(self.property("studioDialogChrome"))
                seen["legacy"] = isinstance(self, qt.QMessageBox) if hasattr(qt, "QMessageBox") else False
                self.findChild(qt.QPushButton, button).click()
                return self.result()

            with mock.patch.object(qt.QDialog, "exec_", run, create=True), mock.patch.object(qt.QDialog, "exec", run, create=True):
                self.assertEqual(card_clusters.confirmFetch(_Manager(), ["a"]), expected)
            self.assertTrue(seen["chrome"], "the confirmation lacks the Studio dialog chrome")
            self.assertFalse(seen["dialog"].inherits("QMessageBox"), "the legacy QMessageBox came back")


if __name__ == "__main__":
    unittest.main()

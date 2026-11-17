"""Test that the "Build fast index" and "Verify against Lead-DBS" dialogs fit a short screen (BB-1).

At 200% scaling on a 1920x1080 screen (960x540 logical) the fast index dialog opened 760 high and the Lead-DBS parity dialog had
a fixed 760x720 minimum size, so their Close and Verify buttons were off screen (bug
lnm-reference-fastindex-paths:manager-dialogs-taller-than-screen-at-2x). Both now keep to 90% of the available area, and the
parity dialog's content scrolls.

Needs the Slicer application (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingManagerDialogFitUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class LesionNetworkMappingManagerDialogFitUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome

        cls.tmp = tempfile.mkdtemp(prefix="lnm dialog fit ")
        info = connectome.make_synthetic(os.path.join(cls.tmp, "S.lnmc"), shape=(12, 14, 12), n_subjects=4, runs=(12,), seed=4, name="S")
        cls.bundle = connectome.ConnectomeBundle.open(info["path"])

    @classmethod
    def tearDownClass(cls):
        cls.bundle.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def make(self, kind, area):
        from LesionNetworkMappingUI import manager_fastindex, manager_parity, widgets

        ctx = types.SimpleNamespace(cohortMaps=None, bundle=self.bundle)
        cls = manager_fastindex.FastIndexDialog if kind == "fast" else manager_parity.ParityDialog
        with mock.patch.object(widgets, "availableArea", return_value=area):
            dialog = cls(ctx, self.bundle.path)
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_short_screen_keeps_both_dialogs_inside_it(self):
        area = qt.QRect(0, 0, 960, 540)
        for kind in ("fast", "parity"):
            dialog = self.make(kind, area)
            self.assertLessEqual(dialog.height, area.height(), kind)
            self.assertLessEqual(dialog.width, area.width(), kind)
            self.assertLessEqual(dialog.minimumHeight, 540 * 0.9, kind)
            self.assertLessEqual(dialog.minimumSizeHint.height(), area.height(), kind + ": the content must scroll, not force the dialog taller than the screen")

    def test_large_screen_keeps_the_default_sizes(self):
        area = qt.QRect(0, 0, 2560, 1440)
        fast = self.make("fast", area)
        self.assertEqual((fast.width, fast.height), (660, 760))
        parity = self.make("parity", area)
        self.assertEqual((parity.width, parity.height), (760, 720))

    def test_no_screen_keeps_the_default_sizes(self):
        self.assertEqual(self.make("fast", None).height, 760)
        self.assertEqual(self.make("parity", None).height, 720)

    def test_parity_subjects_combo_shows_its_text(self):
        dialog = self.make("parity", None)
        combo = dialog.subsetCombo
        self.assertGreater(combo.count, 0)
        fm = qt.QFontMetrics(combo.font)
        text = combo.itemText(0)
        width = fm.horizontalAdvance(text) if hasattr(fm, "horizontalAdvance") else fm.width(text)
        self.assertGreaterEqual(combo.sizeHint.width(), width + 40, "the Subjects combo must be wide enough for '%s'" % text)

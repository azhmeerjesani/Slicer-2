"""Test that the Cohort tabs' "Design preview…" popup shows its whole content (bug contrast-design-preview-cropped-1366).

On a 1366x768 screen the popup came out about 554 px tall instead of its 686 px size hint: ``QWidget.adjustSize`` bounds
a window to about two thirds of the screen height, so the design-matrix heat map was cropped and the facts table lost
its "Smallest attainable p" and "Permutation" rows. The popup is now sized to its content (up to the screen's available
height) and scrolls when the content is taller than the screen. The tests size the content relative to the screen
they run on, so they hold at any screen size.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingDesignPreviewFitUITest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import time
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")

import numpy as np
import qt


class _Design:
    """The fields of ``design.Design`` that the preview reads."""

    design_type = "two_sample"
    columns = ["group", "intercept", "volume_ml", "age", "sex"]
    n_per_level = {"case": 18, "control": 18}
    excluded = {}
    n = 36
    rank = 5
    p = 5
    vif = {"volume_ml": 1.12, "age": 1.08, "sex": 1.05}
    unique_perms = 9075135300
    n_perm = 5000
    exhaustive = False
    min_p = 0.0002
    permute_reason = "Freedman-Lane: nuisance covariates are present, so the residuals of the reduced model are permuted"
    checks = ()

    def __init__(self, limitations):
        rng = np.random.default_rng(0)
        self.X = np.column_stack([np.repeat([1.0, -1.0], 18), np.ones(36), rng.normal(size=(36, 3))])
        self.limitations = limitations


def pump(ms=100):
    end = time.time() + ms / 1000.0
    while time.time() < end:
        slicer.app.processEvents()
        time.sleep(0.01)


class DesignPreviewFitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        slicer.util.selectModule("LesionNetworkMapping")
        from LesionNetworkMappingUI import cohort_contrast

        cls.module = cohort_contrast

    def setUp(self):
        self.host = qt.QWidget()
        self.host.setWindowTitle("Design preview fit test")
        layout = qt.QVBoxLayout(self.host)
        self.anchor = qt.QPushButton("Design preview…")
        layout.addWidget(self.anchor)
        self.preview = self.module.DesignPreview(self.host, "LNMTestDesignPreview")
        screen = qt.QGuiApplication.screens()[0]  # the primary screen
        area = screen.availableGeometry
        self.area = area() if callable(area) else area

    def tearDown(self):
        self.preview.close()
        self.host.close()
        self.host.deleteLater()
        pump(50)

    def naturalHeight(self):
        """The popup's height with nothing cropped or scrolled."""
        contentHeight = getattr(self.preview, "contentHeight", None)
        if contentHeight is not None:
            return contentHeight()
        hint = self.preview.frame.heightForWidth(self.preview.frame.width)  # before the fix: no scroll area
        return hint if hint > 0 else self.preview.frame.sizeHint.height()

    def limitationsForHeight(self, fraction):
        """Limitations that make the popup's natural height at least ``fraction`` of the available screen height."""
        limitations = []
        while True:
            self.preview.setDesign(_Design(limitations))
            hint = self.naturalHeight()
            if hint >= fraction * self.area.height() or len(limitations) > 400:
                return limitations, hint
            limitations.append(f"Limitation number {len(limitations) + 1}: a sentence long enough to wrap onto a second line of the popup.")

    def showPreview(self, hostTopLeft=None):
        self.host.move(hostTopLeft or self.area.topLeft())
        self.host.show()
        pump(200)
        self.preview.showAt(self.anchor)
        pump(300)
        self.assertTrue(self.preview.frame.isVisible())

    def assertInsideScreen(self):
        frame = self.preview.frame
        topLeft = frame.mapToGlobal(qt.QPoint(0, 0))
        self.assertGreaterEqual(topLeft.y(), self.area.top())
        self.assertLessEqual(topLeft.y() + frame.height, self.area.bottom() + 1, "the popup reaches below the screen")
        self.assertGreaterEqual(topLeft.x(), self.area.left())
        self.assertLessEqual(topLeft.x() + frame.width, self.area.right() + 1)
        self.assertLessEqual(frame.height, self.area.height())

    def assertNothingSqueezed(self):
        """Every label gets the height its wrapped text (or the heat map) needs: nothing is cropped."""
        preview = self.preview
        pixmap = preview.image.pixmap
        self.assertIsNotNone(pixmap)
        self.assertGreaterEqual(preview.image.height, pixmap.height(), "the heat map is cropped")
        for widget in (preview.typeLabel, preview.columnsLabel, preview.factsLabel, preview.limitationsLabel):
            if widget.isVisible():
                needed = widget.heightForWidth(widget.width)
                self.assertGreaterEqual(widget.height, needed, f"{widget.objectName} is squeezed")

    def test_content_that_fits_the_screen_is_shown_whole(self):
        _limitations, hint = self.limitationsForHeight(0.8)
        self.assertLess(hint, self.area.height() - 20, "the test content should fit the screen")
        self.showPreview()
        self.assertInsideScreen()
        self.assertNothingSqueezed()
        self.assertGreaterEqual(self.preview.frame.height, hint)
        self.assertIn("Smallest attainable p", self.preview.factsLabel.text)
        self.assertIn("Permutation</b>", self.preview.factsLabel.text)
        scroll = getattr(self.preview, "scroll", None)
        if scroll is not None:
            self.assertFalse(scroll.verticalScrollBar().isVisible(), "content that fits should not scroll")

    def test_content_taller_than_the_screen_scrolls(self):
        _limitations, hint = self.limitationsForHeight(1.3)
        self.assertGreater(hint, self.area.height())
        self.showPreview()
        self.assertInsideScreen()
        self.assertNothingSqueezed()
        scroll = getattr(self.preview, "scroll", None)
        self.assertIsNotNone(scroll, "taller content needs a scroll area")
        bar = scroll.verticalScrollBar()
        self.assertTrue(bar.isVisible())
        bar.setValue(bar.maximum)
        pump(100)
        self.assertGreater(bar.maximum, 0)

    def test_anchor_out_of_view_keeps_the_popup_on_screen(self):
        self.limitationsForHeight(0.3)
        self.showPreview(qt.QPoint(self.area.left(), self.area.bottom() + 1000))
        self.assertInsideScreen()


if __name__ == "__main__":
    unittest.main()

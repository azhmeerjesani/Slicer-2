"""Test that the Calibrate card's form has no blank bands (BB-1).

The form was a QFormLayout holding word-wrapped rows (the design and target captions, the wrapped sensitivity check box). It
gave them heights computed for another width: a band of about 110 px between the check box and the Calibrate button, and a
blank line in each caption (bug pipeline-calibration-controls-gui:calibrate-card-form-blank-gaps).

It needs the Slicer application (the card is built on a stand-in Map context; nothing is computed)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingCalibrateFormLayoutTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import unittest

try:
    import qt
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class _Ctx:
    bundle = None
    study = None
    runner = None

    def __init__(self):
        self.bundleChanged = []
        self.studyChanged = []

    def card(self, key):
        return None


class LesionNetworkMappingCalibrateFormLayoutTest(unittest.TestCase):
    def test_no_blank_band_or_blank_caption_lines(self):
        from LesionNetworkMappingUI import card_calibrate
        card = card_calibrate.CalibrateCard(_Ctx())
        card.designLabel.setText("Design: contrast, group column diagnosis, covariates: age, sex, volume_ml (log), permutation: within blocks")
        card.targetLabel.setText("Target: the network of lesion L12 (median volume), top 5% of its Fz map, 4,321 voxels")
        for width in (300, 400, 600):
            host = qt.QWidget()
            layout = qt.QVBoxLayout(host)
            layout.addWidget(card.widget)
            card.widget.collapsed = False
            host.resize(width, 100)
            # The natural height of the card at this width: a taller host would hand the surplus to the rows.
            host.resize(width, layout.totalHeightForWidth(width))
            host.show()
            slicer.app.processEvents()
            try:
                def top(widget, host=host):
                    return widget.mapTo(host, qt.QPoint(0, 0)).y()

                for caption in (card.designLabel, card.targetLabel):
                    self.assertLessEqual(caption.height, caption.heightForWidth(caption.width) + 4,
                                         "caption box taller than its text at width %d" % width)
                row = card.sensitivityCheckBox.parentWidget()
                gap = top(card.runButton) - (top(row) + row.height)
                self.assertLessEqual(gap, 20, "blank band of %d px above the Calibrate button at width %d" % (gap, width))
            finally:
                layout.removeWidget(card.widget)
                card.widget.setParent(None)
                host.deleteLater()


if __name__ == "__main__":
    unittest.main()

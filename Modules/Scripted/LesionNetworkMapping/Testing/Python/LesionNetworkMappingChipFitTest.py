"""A status chip never draws narrower than its text (cosmetic bug: 'Plan: locked ...' clipped at both ends).

The label's own minimum size ignores the stylesheet padding, so a crowded row squeezed the chip below its text width
after a runtime text change. It needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingChipFitTest

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


class LesionNetworkMappingChipFitTest(unittest.TestCase):
    def test_chip_keeps_its_text_width_in_a_crowded_row(self):
        from LesionNetworkMappingUI import widgets

        host = qt.QWidget()
        row = qt.QHBoxLayout(host)
        chip = widgets.chip("Plan: open", "neutral")
        row.addWidget(chip)
        row.addWidget(qt.QPushButton("Lock plan"))
        row.addWidget(qt.QToolButton())
        row.addStretch(1)
        host.setFixedWidth(250)
        host.show()
        widgets.setChipLevel(chip, "ok", "Plan: locked 2026-11-14 · e96c3765")
        slicer.app.processEvents()
        host.layout().activate()
        try:
            self.assertGreaterEqual(chip.width, chip.sizeHint.width(), "the chip is narrower than its text")
        finally:
            host.hide()
            host.deleteLater()


if __name__ == "__main__":
    unittest.main()

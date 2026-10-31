"""Tests of the Networks card (card_networks.py): its Yeo-7 / Yeo-17 control, and a table that follows the Result card.

A change of the Result card's rule or sign must reach the Networks table: ``ruleChanged`` is notified before the rule is
resolved and a change of sign notifies no rule change, so the card learns it from ``thresholdChanged`` (notified once
the widget's ``resolved`` holds the new threshold). With the card collapsed (no automatic remake) the "Out of date"
chip shows that the table on show is the previous threshold's.

Qt 6 has no ``QButtonGroup.buttonClicked(int)`` signal (only ``idClicked(int)``, which exists since Qt 5.15), so a card
that listens to ``buttonClicked(int)`` never learns about a click on the macOS disk image. On Qt 5 the test removes the
``buttonClicked(int)`` connections first, which is what Qt 6 amounts to, then clicks Yeo-17.

They need the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingNetworksUITest

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


class LesionNetworkMappingNetworksUITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingUI import card_networks

        cls.cn = card_networks
        settings = qt.QSettings()
        cls.storedScheme = settings.value(card_networks.SCHEME_SETTING)
        slicer.util.selectModule("LesionNetworkMapping")
        slicer.app.processEvents()
        cls.card = slicer.modules.LesionNetworkMappingWidget.ctx.card("networks")

    @classmethod
    def tearDownClass(cls):
        settings = qt.QSettings()
        if cls.storedScheme is None:
            settings.remove(cls.cn.SCHEME_SETTING)
        else:
            settings.setValue(cls.cn.SCHEME_SETTING, cls.storedScheme)

    @staticmethod
    def staleChipShown(card):
        """The "Out of date" chip is shown, or, in a collapsed card, is shown when the card is expanded (ctkCollapsibleButton
        keeps a child hidden while collapsed and records the request in its "visibilityToParent" property).
        """
        chip = card.staleChip
        if not chip.isHidden():
            return True
        return bool(chip.property("visibilityToParent"))

    def test_result_rule_and_sign_change_reach_the_table(self):
        import os
        import shutil
        import tempfile

        import numpy as np

        from LesionNetworkMappingLib import nifti, thresholds

        widget = slicer.modules.LesionNetworkMappingWidget
        card = self.card
        folder = tempfile.mkdtemp(prefix="lnm-networks-rule-")
        try:
            affine = np.diag([2.0, 2.0, 2.0, 1.0])
            affine[:3, 3] = -20.0
            data = np.zeros((20, 20, 20), dtype=np.float32)
            data[2:18, 2:18, 2:18] = np.random.default_rng(0).normal(0.0, 4.0, (16, 16, 16))
            path = os.path.join(folder, "synth_conn-Test_desc-T_funcmap.nii.gz")
            nifti.write_nifti(path, data, affine)
            roi = np.zeros(data.shape, dtype=np.uint8)
            roi[2:10] = 1
            roiPath = os.path.join(folder, "half.nii.gz")
            nifti.write_nifti(roiPath, roi, affine)

            widget.showMapFile(path)
            widget.signGroup.button(0).click()  # both signs
            widget.setRule({"kind": "custom_t", "value": 3.0})
            card.widget.collapsed = True  # no automatic remake: the chip has to say the table is old
            slicer.app.processEvents()
            card.addRoiFile(roiPath)
            self.assertTrue(card.update())
            self.assertEqual((card.source.value, card.source.sign), (3.0, "both"))
            self.assertFalse(self.staleChipShown(card))

            # The rule: the table is out of date at once, and the "Result rule" check box names the new threshold
            widget.setRule({"kind": "custom_t", "value": 4.0})
            slicer.app.processEvents()
            self.assertTrue(card._isStale())
            self.assertTrue(self.staleChipShown(card), "the Networks table of |T| >= 3 is shown as current after the rule became |T| >= 4")
            self.assertIn("4", str(card.sourceBar.ruleCheck.text))
            self.assertTrue(card.update())
            self.assertEqual((card.source.value, card.source.sign), (4.0, "both"))
            self.assertFalse(self.staleChipShown(card))

            # The sign alone (no rule change)
            widget.signGroup.button(1).click()  # Positive
            slicer.app.processEvents()
            self.assertTrue(self.staleChipShown(card), "the Networks table of both signs is shown as current after the sign became Positive")
            self.assertTrue(card.update())
            self.assertEqual((card.source.value, card.source.sign), (4.0, "pos"))
            self.assertTrue(str(card.headerLabel.text).startswith("T ≥ 4"), card.headerLabel.text)

            # Expanded, the card remakes the table by itself for the new rule
            card.widget.collapsed = False
            widget.setRule({"kind": "custom_t", "value": 5.0})
            deadline = qt.QTime.currentTime().addMSecs(3000)
            while card.source.value != 5.0 and qt.QTime.currentTime() < deadline:
                slicer.app.processEvents()
            self.assertEqual((card.source.value, card.source.sign), (5.0, "pos"))
        finally:
            card.widget.collapsed = True
            card.rois.clear()
            widget.signGroup.button(0).click()
            widget.setRule(thresholds.DEFAULT_RULE)
            card.schedule()
            slicer.app.processEvents()
            shutil.rmtree(folder, ignore_errors=True)

    def test_scheme_click_without_buttonClicked_int(self):
        card = self.card
        self.assertIsNotNone(card)
        group = card.schemeGroup
        self.assertIsNotNone(slicer.util.findChild(card.widget, "LNMNetworksSchemeSegmented"))
        card.setScheme("yeo7")
        self.assertEqual(card.scheme, "yeo7")
        if not hasattr(group, "idClicked"):
            self.skipTest("Qt before 5.15: no idClicked(int)")
        if hasattr(group, "buttonClicked") and qt.qVersion().startswith("5."):
            group.disconnect("buttonClicked(int)")  # what Qt 6 amounts to: that signal does not exist there
        yeo17 = list(self.cn.SCHEMES).index("yeo17")
        group.button(yeo17).click()
        slicer.app.processEvents()
        self.assertEqual(group.checkedId(), yeo17)
        self.assertEqual(card.scheme, "yeo17")
        self.assertEqual(qt.QSettings().value(self.cn.SCHEME_SETTING), "yeo17")
        group.button(list(self.cn.SCHEMES).index("yeo7")).click()
        slicer.app.processEvents()
        self.assertEqual(card.scheme, "yeo7")


if __name__ == "__main__":
    unittest.main()

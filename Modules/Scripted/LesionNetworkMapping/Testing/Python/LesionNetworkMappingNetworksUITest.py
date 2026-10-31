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

    def test_declared_template_space_off_the_standard_grids(self):
        """A map whose template space the app knows (its sidecar's connectome space, the ``Studio.Space`` of its volume)
        but whose grid is no standard grid (a 4 mm resampled map, the demo connectome's 6 mm grid) gets its Side column,
        its atlas labels and its networks: the cards read that space instead of recognizing the grid alone.
        """
        import json
        import os
        import shutil
        import tempfile

        import numpy as np

        from LesionNetworkMappingLib import atlases, nifti, thresholds
        from LesionNetworkMappingUI import card_clusters

        widget = slicer.modules.LesionNetworkMappingWidget
        clustersCard = widget.ctx.card("clusters")
        card = self.card
        manager = card.atlases
        self.assertIs(manager, clustersCard.atlases)
        folder = tempfile.mkdtemp(prefix="lnm-declared-space-")
        storedOverride = dict(manager.override)
        try:
            # A T map on a 4 mm grid that no registry knows; its sidecar declares the connectome's space
            affine = np.diag([4.0, 4.0, 4.0, 1.0])
            affine[:3, 3] = (-42.0, -50.0, -30.0)
            data = np.zeros((22, 26, 20), dtype=np.float32)
            data[4:8, 10:14, 8:12] = 6.0  # x -26..-14: left
            data[14:18, 10:14, 8:12] = -6.0  # x 14..26: right
            path = os.path.join(folder, "synth4mm_conn-Decl4mm_desc-T_funcmap.nii.gz")
            nifti.write_nifti(path, data, affine)
            with open(os.path.join(folder, "synth4mm_conn-Decl4mm_lnm.json"), "w", encoding="utf-8") as f:
                json.dump({"N": 30, "connectome": {"label": "Decl4mm", "space": "MNI152NLin6Asym"}}, f)
            self.assertEqual(atlases.mask_family(data.shape, affine), ("unknown", None), "the test grid must not be a known grid")

            # Atlases of the FSL family on their own 2 mm grid: left half one network, right half another
            atlasAffine = np.diag([2.0, 2.0, 2.0, 1.0])
            atlasAffine[:3, 3] = (-60.0, -80.0, -60.0)
            labels = np.zeros((61, 81, 61), dtype=np.int32)
            labels[:30] = 1
            labels[31:] = 2
            names = {1: "7Networks_LH_Vis_1", 2: "7Networks_RH_Default_PCC_1"}
            cortex = atlases.Atlas(self.cn.SCHEMES["yeo7"]["cortex"], labels, atlasAffine, names, family="fsl6", space="MNI152NLin6Asym", networks="schaefer")
            labelAtlas = atlases.Atlas(card_clusters.LABEL_ATLASES[0], labels, atlasAffine, names, family="fsl6", space="MNI152NLin6Asym", networks="schaefer")
            manager.override = {cortex.id: cortex, labelAtlas.id: labelAtlas}
            settings = qt.QSettings()
            storedAtlases = settings.value(card_clusters.ATLASES_SETTING)
            settings.setValue(card_clusters.ATLASES_SETTING, json.dumps([labelAtlas.id]))

            widget.showMapFile(path)
            widget.signGroup.button(0).click()  # both signs
            widget.setRule({"kind": "custom_t", "value": 3.0})
            card.setScheme("yeo7")
            slicer.app.processEvents()
            volume = card.model.resultVolume()
            self.assertEqual(volume.GetAttribute("Studio.Space"), "MNI152NLin6Asym")

            table = clustersCard.update()
            self.assertIsNotNone(table)
            self.assertTrue(clustersCard.knownSpace, clustersCard.noticeLabel.text)
            self.assertNotIn("not recognized", str(clustersCard.noticeLabel.text))
            self.assertNotIn("could not be told", str(clustersCard.noticeLabel.text))
            byHemisphere = {row["hemisphere"]: row for row in table.clusters}
            self.assertEqual(set(byHemisphere), {"L", "R"})
            self.assertEqual([label for _atlas, label, _pct in byHemisphere["L"]["labels"]][:1], ["7Networks_LH_Vis_1"])
            self.assertEqual([label for _atlas, label, _pct in byHemisphere["R"]["labels"]][:1], ["7Networks_RH_Default_PCC_1"])
            self.assertEqual({clustersCard._cell(row, "side")[0] for row in table.clusters}, {"Left", "Right"})

            rows = card.update()
            self.assertTrue(rows, card.noticeLabel.text)
            self.assertNotIn("could not be told", str(card.noticeLabel.text))
            cortexRows = {row["name"]: row for row in rows if row["system"] == "cortex"}
            self.assertEqual(set(cortexRows), {"Vis", "Default"})
            self.assertEqual(card.source.space, "MNI152NLin6Asym")

            # A file opened in the cards' own source menu reads the sidecar too
            key = card.model.addFile(path)
            self.assertEqual(card.model.source().space, "MNI152NLin6Asym")
            self.assertTrue(card.update())
            self.assertNotIn("could not be told", str(card.noticeLabel.text))
            card.model.files.pop(key, None)
            card.model.setChoice("auto")
        finally:
            manager.override = storedOverride
            settings = qt.QSettings()
            if "storedAtlases" in locals():
                if storedAtlases is None:
                    settings.remove(card_clusters.ATLASES_SETTING)
                else:
                    settings.setValue(card_clusters.ATLASES_SETTING, storedAtlases)
            card.widget.collapsed = True
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

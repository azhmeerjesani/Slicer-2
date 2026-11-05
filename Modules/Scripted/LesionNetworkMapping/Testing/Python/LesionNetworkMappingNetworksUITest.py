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

import time
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

    def test_header_counts_the_suprathreshold_voxels_when_no_network_is_listed(self):
        """With no unit (no atlas for the map, no ROI) the table is empty, but the header still gives the real counts."""
        import os
        import shutil
        import tempfile

        import numpy as np

        from LesionNetworkMappingLib import nifti, thresholds

        # The totals alone (no units): the statistics function returns no row, the totals function the counts
        values = np.array([6.0, 5.0, -5.0, -7.0, -1.0, 0.0, np.nan, 4.9])
        self.assertEqual(self.cn.networkStatistics(values, [], 5.0, "both"), [])
        self.assertEqual(self.cn.suprathresholdTotals(values, 5.0, "both"), {"supraPos": 2, "supraNeg": 2, "voxels": 7})
        self.assertEqual(self.cn.suprathresholdTotals(values, 5.0, "pos")["supraNeg"], 0)
        units = [{"system": "yeo7", "name": "A", "atlas": None, "positions": np.array([0, 1, 2])}]
        rows = self.cn.networkStatistics(values, units, 5.0, "both")
        self.assertEqual({k: rows[0]["totals"][k] for k in ("supraPos", "supraNeg")}, {"supraPos": 2, "supraNeg": 2})

        widget = slicer.modules.LesionNetworkMappingWidget
        card = self.card
        folder = tempfile.mkdtemp(prefix="lnm-networks-header-")
        try:
            affine = np.diag([2.0, 2.0, 2.0, 1.0])
            affine[:3, 3] = -20.0
            data = np.zeros((20, 20, 20), dtype=np.float32)
            data[2:18, 2:18, 2:18] = np.random.default_rng(1).normal(0.0, 4.0, (16, 16, 16))
            path = os.path.join(folder, "synth_conn-Test_desc-T_funcmap.nii.gz")
            nifti.write_nifti(path, data, affine)
            widget.showMapFile(path)
            widget.signGroup.button(0).click()
            widget.setRule({"kind": "custom_t", "value": 5.0})
            card.widget.collapsed = True
            card.rois.clear()
            slicer.app.processEvents()
            rows = card.update()
            self.assertFalse(rows, "the synthetic map was expected to have no network to list")
            self.assertEqual(int((data >= 5).sum()) > 0 and int((data <= -5).sum()) > 0, True)
            header = str(card.headerLabel.text)
            self.assertIn(f"{int((data >= 5).sum()):,} voxels positive", header)
            self.assertIn(f"{int((data <= -5).sum()):,} negative", header)
        finally:
            card.widget.collapsed = True
            card.rois.clear()
            widget.setRule(thresholds.DEFAULT_RULE)
            card.schedule()
            slicer.app.processEvents()
            shutil.rmtree(folder, ignore_errors=True)

    def test_nct_export_into_a_used_folder_leaves_no_volume_of_the_earlier_export(self):
        """Exporting both signs and then one sign into the same folder: the folder holds only the files the new sidecar lists."""
        import json
        import os
        import shutil
        import tempfile
        from types import SimpleNamespace

        import numpy as np

        grid = self.cn.nctGrid()
        volume = np.zeros(grid.shape, dtype=np.float32)
        volume[10, 10, 10] = 8.0
        volume[20, 20, 20] = -8.0
        folder = tempfile.mkdtemp(prefix="lnm-nct-")
        try:

            def export(sign):
                source = SimpleNamespace(grid=grid, volume=volume, value=5.0, sign=sign, statistic="T", space=None)
                self.cn.writeNct(source, folder)
                with open(os.path.join(folder, "networks_nct.json"), encoding="utf-8") as f:
                    listed = json.load(f)["files"]
                volumes = sorted(name for name in os.listdir(folder) if name.endswith(".nii.gz"))
                self.assertEqual(volumes, sorted(listed), sign)
                return volumes

            self.assertEqual(len(export("both")), 4)
            # One sign: the binary mask and the thresholded map only; the earlier suprapos / supraneg are gone
            for sign in ("pos", "neg"):
                self.assertEqual(export(sign), ["networks_nct_desc-supra.nii.gz", "networks_nct_desc-thresholded.nii.gz"])
            self.assertEqual(len(export("both")), 4)
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def test_roi_rows_leave_the_not_in_these_networks_row_unchanged(self):
        """An ROI is an extra row: the voxels in it but in no network stay in "not in these networks" (rows add up to 100 %)."""
        import numpy as np

        # 12 voxels: network A holds 0-3, network B 4-5; the ROI holds 3-7 (one voxel of A, both of B, two outside every network)
        values = np.array([6.0, 1.0, 1.0, 7.0, -6.0, 0.0, 8.0, 9.0, 5.5, -7.0, 0.5, np.nan])
        networks = [{"system": "cortex", "name": "A", "atlas": None, "positions": np.array([0, 1, 2, 3])},
                    {"system": "cortex", "name": "B", "atlas": None, "positions": np.array([4, 5])}]
        roi = [{"system": "roi", "name": "box", "atlas": None, "positions": np.array([3, 4, 5, 6, 7])}]
        before = self.cn.networkStatistics(values, networks, 5.0, "both")
        after = self.cn.networkStatistics(values, [*networks, *roi], 5.0, "both")
        otherBefore = [row for row in before if row["system"] == "other"]
        otherAfter = [row for row in after if row["system"] == "other"]
        self.assertEqual(len(otherBefore), 1)
        self.assertEqual(len(otherAfter), 1)
        self.assertEqual(otherAfter[0], otherBefore[0])
        # numpy: voxels 6-10 are in no network (11 is NaN); positive 6, 7, 8 of the 5 positive (0, 3, 6, 7, 8); negative 9 of 2
        self.assertEqual(otherAfter[0]["voxels"], 5)
        self.assertAlmostEqual(otherAfter[0]["pctPos"], 60.0)
        self.assertAlmostEqual(otherAfter[0]["pctNeg"], 50.0)
        self.assertAlmostEqual(otherAfter[0]["mean"], float(np.mean(values[6:11])))
        self.assertEqual([row["name"] for row in after], ["A", "B", "box", self.cn._("not in these networks")])
        roiRow = after[2]
        self.assertEqual((roiRow["system"], roiRow["voxels"]), ("roi", 5))
        for key in ("pctPos", "pctNeg"):
            self.assertAlmostEqual(sum(row[key] for row in after if row["system"] != "roi"), 100.0, msg=key)
        # Without a network unit (only ROIs) there is no "not in these networks" row
        self.assertEqual([row["system"] for row in self.cn.networkStatistics(values, roi, 5.0, "both")], ["roi"])

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

    def test_group_map_keeps_the_connectome_declared_space(self):
        """A group map of a connectome on a grid that is not standard (the demo's 6 mm grid, a 4 mm conversion) is in the
        template space its connectome.json declares, as that connectome's result map is: the Cohort card's group map
        volume (Overlap tab, the Contrast tab's maps, the Nulls maps) carries it in ``Studio.Space``, so the Clusters and
        Networks cards label it and list its networks instead of "the space of the map could not be told".
        """
        import os
        import shutil
        import tempfile
        from types import SimpleNamespace
        from unittest import mock

        import numpy as np

        from LesionNetworkMappingLib import atlases, nifti
        from LesionNetworkMappingUI import cohort_contrast

        widget = slicer.modules.LesionNetworkMappingWidget
        cohortCard = widget.ctx.card("cohort")
        clustersCard = widget.ctx.card("clusters")
        folder = tempfile.mkdtemp(prefix="lnm-group-declared-space-")
        storedOutputs = dict(cohortCard.overlap.outputs)
        storedChoice = clustersCard.model.choice
        try:
            # Group maps on a 4 mm grid that no registry knows; the connectome declares its template space
            affine = np.diag([4.0, 4.0, 4.0, 1.0])
            affine[:3, 3] = (-42.0, -50.0, -30.0)
            shape = (22, 26, 20)
            self.assertEqual(atlases.mask_family(shape, affine), ("unknown", None), "the test grid must not be a known grid")
            data = np.zeros(shape, dtype=np.float32)
            data[4:8, 10:14, 8:12] = 80.0  # x -26..-14: left
            path = os.path.join(folder, "group_conn-Decl4mm_desc-overlapPct_map.nii.gz")
            nifti.write_nifti(path, data, affine)
            bundle = SimpleNamespace(name="Decl4mm", path=folder, shape=shape, affine=affine, metadata={"space": "MNI152NLin2009cAsym"})
            self.assertEqual(widget.logic.connectomeSpaceName(bundle), "MNI152NLin2009cAsym")

            class Context:
                """The cards' context with the connectome above."""

                def __init__(self, ctx):
                    self._ctx = ctx
                    self.bundle = bundle

                def __getattr__(self, name):
                    return getattr(self._ctx, name)

            context = Context(cohortCard.ctx)
            cohortCard.overlap.outputs = {"overlapPct": path}
            with mock.patch.object(cohortCard, "ctx", context):
                volume = cohortCard.showGroupMap("overlapPct")
            slicer.app.processEvents()
            self.assertIsNotNone(volume)
            self.assertEqual(volume.GetAttribute("Studio.Space"), "MNI152NLin2009cAsym")
            clustersCard.model.setChoice("group")
            self.assertEqual(clustersCard.model.source().space, "MNI152NLin2009cAsym")
            table = clustersCard.update()
            self.assertIsNotNone(table, clustersCard.noticeLabel.text)
            self.assertTrue(clustersCard.knownSpace, clustersCard.noticeLabel.text)
            self.assertNotIn("could not be told", str(clustersCard.noticeLabel.text))

            # The Contrast tab's group maps go to the same volume, with the same space
            volume.RemoveAttribute("Studio.Space")
            shown = cohort_contrast.showGroupFile(context, path, "overlapPct", "Contrast overlapPct")
            slicer.app.processEvents()
            self.assertIsNotNone(shown)
            self.assertEqual(shown.GetAttribute("Studio.Space"), "MNI152NLin2009cAsym")

            # A connectome that declares no template space (native) leaves none on the reused volume
            bundle.metadata = {"space": "native"}
            with mock.patch.object(cohortCard, "ctx", context):
                volume = cohortCard.showGroupMap("overlapPct")
            self.assertIsNone(volume.GetAttribute("Studio.Space"))
        finally:
            cohortCard.overlap.outputs = storedOutputs
            clustersCard.model.setChoice(storedChoice)
            for node in list(slicer.util.getNodesByClass("vtkMRMLScalarVolumeNode")):
                if node.GetAttribute(cohort_contrast.GROUP_VOLUME_ATTRIBUTE) == "1":
                    slicer.mrmlScene.RemoveNode(node)
            cohortCard.groupVolumeId = None
            slicer.app.processEvents()
            shutil.rmtree(folder, ignore_errors=True)

    def test_2009_family_map_uses_the_2009c_atlases(self):
        """A map of the 2009 template family (the bundled MNI152NLin2009cAsym template's) is labelled and split into
        networks with the catalog's 2009c atlases (Schaefer, Tian S1), which the Atlases menu and Get atlases offer;
        the fsl6 atlases are not used for it (families are not mixed), and the catalog has no 2009c Buckner cerebellum.
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
        folder = tempfile.mkdtemp(prefix="lnm-2009c-atlases-")
        storedOverride = dict(manager.override)
        settings = qt.QSettings()
        keys = list(card_clusters.ATLASES_SETTINGS.values())
        storedSettings = {key: settings.value(key) for key in keys}
        try:
            affine = np.diag([2.0, 2.0, 2.0, 1.0])
            affine[:3, 3] = -20.0
            data = np.full((20, 20, 20), 0.5, dtype=np.float32)  # every voxel in the map (no connectome mask)
            data[2:6, 8:12, 8:12] = 6.0  # x -16..-10: left
            data[14:18, 8:12, 8:12] = -6.0  # x 8..14: right
            path = os.path.join(folder, "synth2009_conn-Fam2009_desc-T_funcmap.nii.gz")
            nifti.write_nifti(path, data, affine)
            with open(os.path.join(folder, "synth2009_conn-Fam2009_lnm.json"), "w", encoding="utf-8") as f:
                json.dump({"N": 30, "connectome": {"label": "Fam2009", "space": "MNI152NLin2009cAsym"}}, f)

            atlasAffine = np.diag([2.0, 2.0, 2.0, 1.0])
            atlasAffine[:3, 3] = (-60.0, -80.0, -60.0)
            cortexLabels = np.zeros((61, 81, 61), dtype=np.int32)
            cortexLabels[:30] = 1
            cortexLabels[31:] = 2
            names = {1: "7Networks_LH_Vis_1", 2: "7Networks_RH_Default_PCC_1"}
            subcortexLabels = np.zeros((61, 81, 61), dtype=np.int32)
            subcortexLabels[28:32, 36:40, 28:32] = 1  # x -4..2: between the two blobs
            schaefer2009 = atlases.Atlas("schaefer2018-400-7n-2mm-2009c", cortexLabels, atlasAffine, names, family="2009", space="MNI152NLin2009cAsym", networks="schaefer")
            tian2009 = atlases.Atlas("tian-s1-2mm-2009c", subcortexLabels, atlasAffine, {1: "aHIP-lh"}, family="2009", space="MNI152NLin2009cAsym", networks="tian")
            # An fsl6 atlas on this computer too: it must not label a 2009 map
            schaeferFsl6 = atlases.Atlas("schaefer2018-400-7n-2mm", cortexLabels, atlasAffine, {1: "7Networks_LH_SomMot_1", 2: "7Networks_RH_SomMot_1"}, family="fsl6", space="MNI152NLin6Asym", networks="schaefer")
            manager.override = {atlas.id: atlas for atlas in (schaefer2009, tian2009, schaeferFsl6)}
            settings.remove(card_clusters.ATLASES_SETTINGS["2009"])
            settings.setValue(card_clusters.ATLASES_SETTING, json.dumps(["harvard-oxford-cortical-th25-2mm"]))  # an fsl6 choice

            widget.showMapFile(path)
            widget.signGroup.button(0).click()  # both signs
            widget.setRule({"kind": "custom_t", "value": 3.0})
            card.setScheme("yeo7")
            slicer.app.processEvents()
            self.assertEqual(card.model.resultVolume().GetAttribute("Studio.Space"), "MNI152NLin2009cAsym")

            # Clusters: the 2009c atlases are offered, chosen by default and label the clusters
            self.assertEqual(clustersCard.atlasFamily(), "2009")
            self.assertEqual(list(clustersCard.labelAtlasIds()), ["schaefer2018-400-7n-2mm-2009c", "tian-s1-2mm-2009c"])
            self.assertEqual(clustersCard.usableAtlasIds(), ["schaefer2018-400-7n-2mm-2009c", "tian-s1-2mm-2009c"])
            table = clustersCard.update()
            self.assertIsNotNone(table)
            byHemisphere = {row["hemisphere"]: row for row in table.clusters}
            self.assertEqual(set(byHemisphere), {"L", "R"})
            self.assertEqual([label for _atlas, label, _pct in byHemisphere["L"]["labels"]][:1], ["7Networks_LH_Vis_1"], clustersCard.noticeLabel.text)
            self.assertEqual([label for _atlas, label, _pct in byHemisphere["R"]["labels"]][:1], ["7Networks_RH_Default_PCC_1"], clustersCard.noticeLabel.text)
            self.assertNotIn("SomMot", json.dumps([row["labels"] for row in table.clusters]))
            self.assertIn("2009c", str(clustersCard.atlasLabel.text))
            clustersCard._fillAtlasMenu()
            entries = [str(action.text) for action in clustersCard.atlasMenu.actions() if str(action.text)]
            self.assertIn(card_clusters.shortName("schaefer2018-400-7n-2mm-2009c"), entries)
            self.assertIn(card_clusters.shortName("tian-s1-2mm-2009c"), entries)
            self.assertFalse([entry for entry in entries if entry.startswith(card_clusters.shortName("schaefer2018-400-7n-2mm"))
                              and "2009c" not in entry], entries)
            # A choice for the 2009 family is kept apart from the fsl6 one
            clustersCard.setSelectedAtlasIds(["tian-s1-2mm-2009c", "harvard-oxford-cortical-th25-2mm"])
            self.assertEqual(json.loads(settings.value(card_clusters.ATLASES_SETTINGS["2009"])), ["tian-s1-2mm-2009c"])
            self.assertEqual(json.loads(settings.value(card_clusters.ATLASES_SETTING)), ["harvard-oxford-cortical-th25-2mm"])
            self.assertEqual(clustersCard.usableAtlasIds(), ["tian-s1-2mm-2009c"])

            # Both atlases unchecked in Atlases...: they are still on this computer, so the label must not say they are missing
            clustersCard.setSelectedAtlasIds([])
            clustersCard._refreshAtlases()
            self.assertEqual(clustersCard.usableAtlasIds(), [])
            self.assertNotIn("no atlas on this computer", str(clustersCard.atlasLabel.text))
            self.assertIn("no atlas chosen", str(clustersCard.atlasLabel.text))
            self.assertFalse(clustersCard.atlasBanner.isVisible())
            # Nothing cached: the old wording stays
            manager.override = {}
            clustersCard.atlases.isCached("tian-s1-2mm-2009c", refresh=True)
            clustersCard._refreshAtlases()
            self.assertIn("no atlas on this computer", str(clustersCard.atlasLabel.text))
            manager.override = {atlas.id: atlas for atlas in (schaefer2009, tian2009, schaeferFsl6)}
            clustersCard.setSelectedAtlasIds(["tian-s1-2mm-2009c"])

            # Networks: cortex from the 2009c Schaefer, subcortex from the 2009c Tian; no 2009c cerebellum in the catalog
            self.assertEqual(card.atlasIds(), ["schaefer2018-400-7n-2mm-2009c", "tian-s1-2mm-2009c"])
            rows = card.update()
            self.assertTrue(rows, card.noticeLabel.text)
            self.assertEqual({row["name"] for row in rows if row["system"] == "cortex"}, {"Vis", "Default"})
            self.assertEqual({row["name"] for row in rows if row["system"] == "subcortex"}, {"HIP"})
            self.assertEqual({row["atlas"] for row in rows if row["system"] in ("cortex", "subcortex")}, {"schaefer2018-400-7n-2mm-2009c", "tian-s1-2mm-2009c"})
            self.assertIn("Cerebellum", str(card.noticeLabel.text))
            card.setScheme("yeo17")
            self.assertEqual(card.atlasIds(), ["schaefer2018-400-17n-2mm-2009c", "tian-s1-2mm-2009c"])
        finally:
            manager.override = storedOverride
            for key, value in storedSettings.items():
                if value is None:
                    settings.remove(key)
                else:
                    settings.setValue(key, value)
            card.setScheme("yeo7")
            card.widget.collapsed = True
            widget.signGroup.button(0).click()
            widget.setRule(thresholds.DEFAULT_RULE)
            card.schedule()
            slicer.app.processEvents()
            shutil.rmtree(folder, ignore_errors=True)

    def test_clusters_peak_coordinates_round_half_away_from_zero(self):
        """On a grid with voxel centres at .5 mm (2009c 2 mm) the Clusters table, tooltip and detail line must round every
        coordinate the same way and never print "-0" (``:.0f`` rounds half to even: 19.5 gave 20 but -58.5 gave -58).
        """
        from types import SimpleNamespace

        from LesionNetworkMappingUI import card_clusters

        fmt = card_clusters.formatCoordinate
        self.assertEqual([fmt(v) for v in (-0.5, 0.5, -0.0, -0.3, 0.0, 19.5, -58.5, -40.5, 39.5, 31.5, -31.5, 2.5, -2.5, 51.4)], ["-1", "1", "0", "0", "0", "20", "-59", "-41", "40", "32", "-32", "3", "-3", "51"])
        row = {"id": 2, "voxels": 26, "mm3": 208.0, "xyz": (-0.5, -58.5, 39.5), "subpeak": False, "hemisphere": "L", "labels": [], "network": "", "sign": "neg"}
        stub = SimpleNamespace(knownSpace=True)
        self.assertEqual([card_clusters.ClustersCard._cell(stub, row, key)[0] for key in "xyz"], ["-1", "-59", "40"])
        text = card_clusters.ClustersCard.detailText(stub, row)
        self.assertIn("peak at -1, -59, 40", text)
        self.assertNotIn("-0", text)

    def test_atlas_download_failure_banner_is_a_readable_sentence(self):
        """Get atlases without a network: the Clusters banner says in one sentence what happened, with no exception class names,
        atlas ids, URLs or urllib3 chains (they were clipped mid-word and ran below the fold).
        """
        from unittest import mock

        from LesionNetworkMappingLib import fetch
        from LesionNetworkMappingUI import card_clusters

        reason = "No connection to templateflow.s3.amazonaws.com: the proxy server cannot be reached. Check the network and try again."
        self.assertEqual(card_clusters.AtlasManager.failureText([], 2), "")
        self.assertEqual(card_clusters.AtlasManager.failureText([reason, reason], 2), "The atlases could not be downloaded. " + reason)
        self.assertEqual(card_clusters.AtlasManager.failureText([reason], 2), "Some atlases could not be downloaded. " + reason)

        manager = card_clusters.AtlasManager()
        ids = ["schaefer2018-400-7n-2mm-2009c", "tian-s1-2mm-2009c"]

        def fail(resource, progress=None, **kwargs):
            raise fetch.OfflineError(reason + " A partial download is kept and resumes next time.")

        with mock.patch.object(card_clusters.AtlasManager, "isCached", return_value=False), mock.patch.object(fetch, "fetch_set", fail):
            self.assertTrue(manager.fetch(ids))
            deadline = time.time() + 30
            while manager.fetching is not None and time.time() < deadline:  # cleared by the poll timer when the job is done
                slicer.app.processEvents()
                time.sleep(0.05)
        self.assertIsNone(manager.fetching)
        self.assertTrue(manager.message.startswith("The atlases could not be downloaded. " + reason), manager.message)
        for raw in ("OfflineError", "schaefer2018", "tian-s1", "https://", "HTTPSConnectionPool"):
            self.assertNotIn(raw, manager.message)
        self.assertLess(len(manager.message), 400)
        self.assertEqual(manager.message.count(reason), 1)  # the same reason of two atlases is said once

        # The banner breaks a long host name instead of clipping it at the edge of the label
        from LesionNetworkMappingUI import widgets

        frame = widgets.banner("short words stay as they are", level="warn")
        label = frame.findChild(qt.QLabel, "LNMBannerText")
        self.assertEqual(label.text, "short words stay as they are")
        widgets.setBannerText(frame, manager.message)
        broken = label.text
        self.assertIn("templateflow.\u200bs3.\u200bamazonaws.\u200bcom", broken)
        self.assertEqual(broken.replace("\u200b", ""), manager.message)

    def test_cohort_roi_chart_reads_lesion_maps_with_the_result_rule_for_a_percent_group_map(self):
        """The cohort ROI chart reads each lesion's T map. With a count or percentage group map in the cards (the overlapPct
        map shown after a cohort run), the map's threshold (overlapPct >= 25) is not on the T scale: each lesion's map is
        read with the Result rule and its sign. A typed threshold of a T map still applies to the lesion maps as it is.
        """
        import os
        import shutil
        import tempfile
        from types import SimpleNamespace
        from unittest import mock

        import numpy as np

        from LesionNetworkMappingLib import geometry, nifti, thresholds
        from LesionNetworkMappingUI import card_clusters

        widget = slicer.modules.LesionNetworkMappingWidget
        card = self.card
        folder = tempfile.mkdtemp(prefix="lnm-networks-roichart-")
        shape = (10, 10, 10)
        affine = np.diag([6.0, 6.0, 6.0, 1.0])
        grid = geometry.Grid(shape, affine)
        indices = np.arange(int(np.prod(shape)), dtype=np.int64)
        box = np.zeros(shape, dtype=bool)
        box[3:6, 3:6, 3:6] = True
        positions = np.flatnonzero(box.reshape(-1))
        rng = np.random.default_rng(7)
        paths = []
        for index in range(6):  # lesions 1-2 positive, 3 negative in the box (connected), 4-6 |T| < 1.5 everywhere
            data = rng.uniform(-1.5, 1.5, shape).astype(np.float32)
            if index < 3:
                data[box] = 8.0 if index < 2 else -8.0
            path = os.path.join(folder, f"sub-{index + 1:02d}_desc-T_funcmap.nii.gz")
            nifti.write_nifti(path, data, affine)
            paths.append(path)
        rows = [{"participant_id": f"sub-{index + 1:02d}", "T": path} for index, path in enumerate(paths)]
        mapsTsv = os.path.join(folder, "maps.tsv")
        bundle = SimpleNamespace(mask_indices=indices, n_subjects=120, n_voxels=int(indices.size), shape=shape)
        units = [{"system": "roi", "name": "box", "atlas": None, "positions": positions}]

        class Context:
            """The card's context with the small connectome above (the other cards keep the real one)."""

            def __init__(self, ctx):
                self._ctx = ctx
                self.bundle = bundle

            def __getattr__(self, name):
                return getattr(self._ctx, name)

        def chart(source):
            card.roiResult = None
            with mock.patch.object(card.model, "source", return_value=source), mock.patch.object(card, "cohortMapPaths", return_value=(rows, "T", mapsTsv)), \
                    mock.patch.object(card, "roiUnits", return_value=(units, [])), mock.patch.object(card, "rois", [{"name": "box"}]), \
                    mock.patch.object(card, "ctx", Context(card.ctx)):
                self.assertEqual(card.runRoiChart(), 6, card.roiLabel.text)
                deadline = time.time() + 30
                while card.roiResult is None and time.time() < deadline:
                    slicer.app.processEvents()
            self.assertIsNotNone(card.roiResult, card.roiLabel.text)
            return card.roiItems()[0], card.roiCaption()

        card.widget.collapsed = True  # no automatic remake of the network table meanwhile
        try:
            widget.signGroup.button(0).click()  # both signs
            widget.setRule(thresholds.DEFAULT_RULE)  # FWE Bonferroni: |T| >= 4.3 at N=120, V=1000
            slicer.app.processEvents()
            volume = np.zeros(shape, dtype=np.float64)
            percent = card_clusters.MapSource("group", "Cohort overlapPct", "overlapPct", volume, grid, indices, 25.0, "pos", 120, False, None, desc="overlapPct")
            item, caption = chart(percent)
            self.assertEqual((item["n_connected"], item["n_lesions"]), (3, 6), f"{item['text']}: {caption}")
            self.assertNotIn("overlapPct", caption)
            self.assertIn("|T| ≥", caption)

            # A typed threshold of a T map is on the lesion maps' scale: it applies as it is (8 < 9: no lesion is connected)
            meanT = card_clusters.MapSource("group", "Cohort meanT", "T", volume, grid, indices, 9.0, "both", 120, False, None, desc="meanT")
            item, caption = chart(meanT)
            self.assertEqual(item["n_connected"], 0, item["text"])
            self.assertIn("|T| ≥ 9", caption)
        finally:
            card.roiResult = None
            card._showRoiChart()
            widget.setRule(thresholds.DEFAULT_RULE)
            slicer.app.processEvents()
            shutil.rmtree(folder, ignore_errors=True)

    def test_group_null_percentiles_of_a_volume_stored_k_flipped(self):
        """The "vs null" percentiles of a group mean-T map read back from a scene or project are those of its file.

        A Nulls run keeps its null maps on a voxel sample: positions into the connectome mask voxels in the connectome's
        storage order (the order of the group map file it wrote). Slicer's volume reader stores the k axis of an FSL-style
        file (diag(-2, 2, 2)) the other way round, so after a scene or project reopen the group map volume holds the same
        voxels in another order. The card must put the map back in the file's (or the open connectome's) order before
        taking the sample; it took the sample from the volume's own order, so the percentiles silently changed.
        """
        import json
        import os
        import shutil
        import tempfile
        from types import SimpleNamespace
        from unittest import mock

        import numpy as np
        import vtk

        from LesionNetworkMappingLib import display, nifti

        widget = slicer.modules.LesionNetworkMappingWidget
        card = self.card
        folder = tempfile.mkdtemp(prefix="lnm-networks-groupnull-")
        shape = (20, 20, 20)
        affine = np.diag([-2.0, 2.0, 2.0, 1.0])  # FSL's LAS storage: Slicer's reader flips its k axis
        affine[:3, 3] = (19.0, -19.0, -19.0)
        rng = np.random.default_rng(11)
        data = np.zeros(shape, dtype=np.float32)
        data[2:18, 2:18, 2:18] = rng.normal(0.5, 1.0, (16, 16, 16))
        data[2:18, 2:18, 2:7] += 2.0  # a bottom slab of higher values: the k order matters
        path = os.path.join(folder, "group_conn-Test_desc-meanT_map.nii.gz")
        nifti.write_nifti(path, data, affine)
        with open(os.path.join(folder, "maps.tsv"), "w", encoding="utf-8") as f:
            f.write("participant_id\tT\n")
        inMask = np.flatnonzero(data.reshape(-1))
        sample = np.sort(rng.choice(inMask.size, size=inMask.size // 3, replace=False)).astype(np.int64)
        k = 49
        # Null maps with the same bottom slab as the map, of random strength: where the slab lands in the volume matters
        slab = np.zeros(shape, dtype=bool)
        slab[:, :, 2:7] = True
        inSlab = slab.reshape(-1)[inMask][sample]
        nullMaps = (rng.normal(0.5, 1.0, (k, sample.size)) + rng.uniform(0.0, 4.0, (k, 1)) * inSlab).astype(np.float32)
        nullsDir = os.path.join(folder, "nulls-rl_independent-exact")
        os.makedirs(nullsDir)
        np.save(os.path.join(nullsDir, "null_group_mean.npy"), nullMaps)
        np.save(os.path.join(nullsDir, "null_group_voxels.npy"), sample)
        with open(os.path.join(nullsDir, "nulls.json"), "w", encoding="utf-8") as f:
            json.dump({"design": {"kind": "rl_independent"}, "null_group_maps": {"file": "null_group_mean.npy", "voxels": "null_group_voxels.npy", "statistic": "T", "shape": [k, int(sample.size)]}}, f)
        rois = {}
        for name, box in (("bottom", np.s_[2:18, 2:18, 2:7]), ("top", np.s_[2:18, 2:18, 13:18]), ("corner", np.s_[2:9, 2:9, 2:9])):
            roi = np.zeros(shape, dtype=np.uint8)
            roi[box] = 1
            rois[name] = os.path.join(folder, f"{name}.nii.gz")
            nifti.write_nifti(rois[name], roi, affine)

        # The reference: the file's in-mask values and the ROIs as positions into them, in the file's order
        position = np.full(data.size, -1, dtype=np.int64)
        position[inMask] = np.arange(inMask.size)
        units = []
        for name, roiPath in rois.items():
            roi, _roiAffine, _header = nifti.read_nifti(roiPath)
            positions = position[np.flatnonzero(np.asarray(roi).reshape(-1))]
            units.append({"system": "roi", "name": name, "atlas": None, "positions": positions[positions >= 0]})
        expected = {unit["name"]: result for unit, result in zip(units, self.cn.nullPercentiles(data.reshape(-1)[inMask].astype(np.float64), units, nullMaps, sample), strict=True)}

        # The group map volume as Slicer reads it back: the k axis stored the other way round
        flipped = affine.copy()
        flipped[:3, 2] = -affine[:3, 2]
        flipped[:3, 3] = affine[:3, 3] + affine[:3, 2] * (shape[2] - 1)
        matrix = vtk.vtkMatrix4x4()
        for row in range(4):
            for column in range(4):
                matrix.SetElement(row, column, float(flipped[row, column]))
        node = slicer.util.addVolumeFromArray(np.ascontiguousarray(np.transpose(data[:, :, ::-1], (2, 1, 0))), ijkToRAS=matrix, name="Cohort meanT (reopened)")
        node.SetAttribute("LNM.GroupMapVolume", "1")
        node.SetAttribute(display.GROUP_MAP_ATTRIBUTE, "meanT")
        stub = SimpleNamespace(shape=shape, affine=affine, mask_indices=inMask, n_voxels=int(inMask.size), n_subjects=100)

        class Context:
            """The source model's context with a connectome on the map file's grid."""

            def __init__(self, ctx):
                self._ctx = ctx
                self.bundle = stub

            def __getattr__(self, name):
                return getattr(self._ctx, name)

        storedChoice, storedMaps = card.model.choice, widget.cohortMapsPath
        card.widget.collapsed = True
        try:
            widget.cohortMapsPath = os.path.join(folder, "maps.tsv")
            card.rois.clear()
            for roiPath in rois.values():
                card.addRoiFile(roiPath)
            card.model.setChoice("group")
            for label, patch in (("without a connectome", mock.patch.object(card.model.ctx, "bundle", None, create=True)),
                                 ("with the connectome open", mock.patch.object(card.model, "ctx", Context(card.model.ctx)))):
                with self.subTest(label), patch:
                    card._nullCache = None
                    rows = card.update()
                    self.assertTrue(rows, card.messageLabel.text)
                    self.assertEqual(card.source.key, "group")
                    self.assertIsNotNone(card.source.path, "the group map file was not found next to maps.tsv")
                    got = {row["name"]: row.get("null") for row in rows if row["system"] == "roi"}
                    self.assertEqual(set(got), set(expected))
                    for name, result in expected.items():
                        self.assertIsNotNone(got[name], f"{label}: no null result for ROI {name}: {card.nullLabel.text}")
                        self.assertNotIn("reason", got[name], f"{label}: {got[name]}")
                        self.assertAlmostEqual(got[name]["pctMean"], result["pctMean"], places=6, msg=f"{label}: ROI {name} pctMean")
                        self.assertAlmostEqual(got[name]["pctR"], result["pctR"], places=6, msg=f"{label}: ROI {name} pctR")
        finally:
            card.widget.collapsed = True
            card.rois.clear()
            card.model.setChoice(storedChoice)
            widget.cohortMapsPath = storedMaps
            card._nullCache = None
            slicer.mrmlScene.RemoveNode(node)
            card.schedule()
            slicer.app.processEvents()
            shutil.rmtree(folder, ignore_errors=True)

    def test_cohort_mean_map_file_opened_reads_its_statistic_and_nulls(self):
        """A cohort's group mean map opened as a file (Networks / Clusters > Open...) reads as the Cohort card's group map does.

        ``group_conn-<label>_desc-meanT_map.nii.gz`` is a mean T map and ``desc-meanFz`` a mean Fz map: the source gets that
        statistic, its default threshold (not the 3 of an unknown "value" map) and the Nulls run of that statistic beside the
        file. The file source knew only the per-lesion names (desc-T, AvgRFz), so both read as "value" and the null cohorts
        were never used ("No Nulls run of the mean value map was found next to this map").
        """
        import json
        import os
        import shutil
        import tempfile

        import numpy as np

        from LesionNetworkMappingLib import nifti

        card = self.card
        folder = tempfile.mkdtemp(prefix="lnm-networks-meanfile-")
        shape = (20, 20, 20)
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        affine[:3, 3] = (-19.0, -19.0, -19.0)
        rng = np.random.default_rng(5)
        storedChoice = card.model.choice
        card.widget.collapsed = True
        keys = []
        try:
            for statistic, desc, scale, threshold in (("T", "meanT", 1.0, 5.0), ("Fz", "meanFz", 0.05, 0.2)):
                with self.subTest(desc):
                    work = os.path.join(folder, statistic, "derivatives", "lnm", "conn-Test", "cohort")
                    nullsDir = os.path.join(work, "nulls-rl_independent-exact")
                    os.makedirs(nullsDir)
                    data = np.zeros(shape, dtype=np.float32)
                    data[2:18, 2:18, 2:18] = rng.normal(0.0, scale, (16, 16, 16))
                    data[4:8, 4:8, 4:8] += 8.0 * scale
                    path = os.path.join(work, f"group_conn-Test_desc-{desc}_map.nii.gz")
                    nifti.write_nifti(path, data, affine)
                    inMask = np.flatnonzero(data.reshape(-1))
                    sample = np.sort(rng.choice(inMask.size, size=inMask.size // 3, replace=False)).astype(np.int64)
                    k = 19
                    np.save(os.path.join(nullsDir, "null_group_mean.npy"), (rng.normal(0.0, scale, (k, sample.size))).astype(np.float32))
                    np.save(os.path.join(nullsDir, "null_group_voxels.npy"), sample)
                    with open(os.path.join(nullsDir, "nulls.json"), "w", encoding="utf-8") as f:
                        json.dump({"design": {"kind": "rl_independent"}, "null_group_maps": {"file": "null_group_mean.npy", "voxels": "null_group_voxels.npy", "statistic": statistic, "shape": [k, int(sample.size)]}}, f)

                    keys.append(card.model.addFile(path))
                    source = card.model.source()
                    self.assertEqual(source.desc, desc)
                    self.assertEqual(source.statistic, statistic, f"the {desc} file read as {source.statistic!r}")
                    self.assertAlmostEqual(source.value, threshold, msg=f"default threshold of the {desc} file")
                    maps, reason = self.cn.findGroupNull(source)
                    self.assertIsNotNone(maps, reason)
                    self.assertEqual(maps.maps.shape[0], k)
                    self.assertEqual(maps.statistic, statistic)
        finally:
            for key in keys:
                card.model.files.pop(key, None)
            card.model.setChoice(storedChoice)
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

"""Test of the p mask of Pipeline card > Load maps (bug pipeline-loadmaps-masks-fwe-p-exactly-alpha).

Load maps blanked the voxels of the T map whose FWE p is not ``< alpha`` on the float32 PFwe map. A permutation p of exactly alpha
(50/1000) reads back as 0.0500000007, so the 2 voxels that the Contrast card, glm.json and the report count as significant
(``glm.passes_alpha``, p <= alpha) were hidden: 3548 instead of 3550 voxels. Load maps now uses the same rule.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingLoadMapsMaskTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import shutil
import tempfile
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class LoadMapsMaskTest(unittest.TestCase):
    def setUp(self):
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")
        self.tmp = tempfile.mkdtemp(prefix="lnm loadmaps mask ")

    def tearDown(self):
        slicer.mrmlScene.Clear(0)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mask_keeps_p_exactly_alpha_in_float32(self):
        import numpy as np

        from LesionNetworkMappingUI import card_pipeline

        p = np.array([[[50 / 1000, 0.049, 0.051, 0.05, 1.0, np.nan, 0.0, 51 / 1000]]], dtype=np.float32)
        self.assertGreater(float(p[0, 0, 0]), 0.05)  # float32 reads back above alpha
        stat = np.full(p.shape, 3.0, dtype=np.float32)
        card_pipeline.maskByP(stat, p, 0.05)
        self.assertEqual(stat.ravel().tolist(), [3.0, 3.0, 0.0, 3.0, 0.0, 0.0, 3.0, 0.0])

    def test_load_one_shows_every_voxel_the_card_counts(self):
        import numpy as np

        from LesionNetworkMappingLib import glm, nifti
        from LesionNetworkMappingUI import card_pipeline

        p = np.array([50 / 1000, 0.049, 0.051, 0.05, 1.0, 0.001, 0.0, 51 / 1000], dtype=np.float32).reshape(8, 1, 1)
        stat = np.arange(1, 9, dtype=np.float32).reshape(8, 1, 1)
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        statPath = os.path.join(self.tmp, "group_desc-caseVsControlTstat_map.nii.gz")
        pPath = os.path.join(self.tmp, "group_desc-caseVsControlPFwe_map.nii.gz")
        nifti.write_nifti(statPath, stat, affine)
        nifti.write_nifti(pPath, p, affine)
        expected = int(np.count_nonzero(glm.passes_alpha(p, 0.05)))
        self.assertEqual(expected, 5)
        volume = card_pipeline.PipelineCard._loadOne(object(), "caseVsControlTstat", statPath, "stat", (pPath, 0.05))
        values = slicer.util.arrayFromVolume(volume).ravel().tolist()  # k, j, i order: the 8 voxels run along i
        self.assertEqual(int(np.count_nonzero(values)), expected, values)
        self.assertEqual(values, [1.0, 2.0, 0.0, 4.0, 0.0, 6.0, 7.0, 0.0])
        self.assertEqual(volume.GetAttribute("LNM.MaskedBy"), "group_desc-caseVsControlPFwe_map.nii.gz <= 0.05")

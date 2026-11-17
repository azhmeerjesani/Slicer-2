"""Test of the N that Map shows for a group map of a pipeline run (bug group-glm-map-opened-in-map-labelled-n-df-plus-1).

``mapFileInfo`` took N of a group map (``group_conn-<label>_desc-<x>_map.nii.gz``) from the T intent (df + 1), which is wrong for a
GLM with covariates (9 lesion maps, 2 groups and one covariate: df 6, N shown 7). It now reads the ``slicer-lnm-group-map`` sidecar
(``n_maps``) and keeps df for the threshold.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingGroupMapNTest
"""

import json
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


class GroupMapNTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="lnm-groupn-")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def _write(self, sidecar):
        import numpy as np

        from LesionNetworkMappingLib import nifti

        path = os.path.join(self.folder, "group_conn-Synthetic_desc-caseVsControlTstat_map.nii.gz")
        nifti.write_nifti(path, np.ones((4, 4, 4), dtype="f4"), np.eye(4), intent_code=3, intent_params=(6.0, 0, 0))
        if sidecar is not None:
            with open(path[: -len(".nii.gz")] + ".json", "w", encoding="utf-8") as f:
                json.dump(sidecar, f)
        return path

    def test_group_sidecar_gives_n(self):
        from LesionNetworkMapping import LesionNetworkMappingLogic

        info = LesionNetworkMappingLogic.mapFileInfo(self._write({"format": "slicer-lnm-group-map", "n_maps": 9}))
        self.assertEqual(info["n"], 9)
        self.assertEqual(info["df"], 6)

    def test_without_sidecar_keeps_df_plus_one(self):
        from LesionNetworkMapping import LesionNetworkMappingLogic

        self.assertEqual(LesionNetworkMappingLogic.mapFileInfo(self._write(None))["n"], 7)

    def test_foreign_sidecar_ignored(self):
        from LesionNetworkMapping import LesionNetworkMappingLogic

        self.assertEqual(LesionNetworkMappingLogic.mapFileInfo(self._write({"format": "other", "n_maps": 9}))["n"], 7)

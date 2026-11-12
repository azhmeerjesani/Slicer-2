"""Test that a map shown in the reused volume does not keep the file name of the previous map
(bug project-map-file-keeps-name-of-first-map).

``logic.loadMap`` reuses one volume for every map shown from a file and renamed it, but the storage node kept the file name
of the first map, so a project saved "Maps/anatA ....nrrd" holding map B. The reused volume now drops the old storage node.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingReusedMapStorageTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import os
import tempfile
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class ReusedMapStorageTest(unittest.TestCase):
    def setUp(self):
        slicer.mrmlScene.Clear(0)

    def tearDown(self):
        slicer.mrmlScene.Clear(0)

    def test_new_map_does_not_keep_old_file_name(self):
        import nibabel as nib
        import numpy as np

        from LesionNetworkMapping import LesionNetworkMappingLogic

        logic = LesionNetworkMappingLogic()
        with tempfile.TemporaryDirectory() as folder:
            paths = []
            for key, value in (("A", 1.0), ("B", 2.0)):
                path = os.path.join(folder, f"{key}_desc-T_funcmap.nii.gz")
                nib.save(nib.Nifti1Image(np.full((3, 3, 3), value, dtype=np.float32), np.eye(4)), path)
                paths.append(path)
            volume = logic.loadMap(paths[0], "A LNM T")
            volume.AddDefaultStorageNode()
            volume.GetStorageNode().SetFileName(os.path.join(folder, "Maps", "A LNM T.nrrd"))
            again = logic.loadMap(paths[1], "B LNM T", volume)
            self.assertIs(again, volume)
            self.assertEqual(again.GetName(), "B LNM T")
            storage = again.GetStorageNode()
            fileName = os.path.basename(storage.GetFileName() or "") if storage is not None else ""
            self.assertNotIn("A LNM T", fileName)
            # No orphan storage node is left in the scene
            self.assertEqual(slicer.mrmlScene.GetNumberOfNodesByClass("vtkMRMLVolumeArchetypeStorageNode"), 0)

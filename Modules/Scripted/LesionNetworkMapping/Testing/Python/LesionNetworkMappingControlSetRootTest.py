"""Test of the control-set registry folder without Library (bug controlset-paths-unresolved-in-map-until-library-card-shown).

Only Library's Control sets card set ``Study.controlset_root`` and ``SLICER_STUDIO_CONTROLSETS``. When a study was opened
in Map (or a job started) before that card was built, linked control lesions (``controlset:<set>/<rel>``) were looked for
in ~/Documents/SlicerStudio/ControlSets, not in <Studio folder>/LNM/ControlSets where Library registers them: every
linked control was reported missing and Run everything was blocked. The module now gives the registry the application's
data directory (``Studio/LNM/DataDirectory`` as the normalization card reads it) and jobs inherit it.

The library part runs under PythonSlicer; the application part needs Slicer::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingControlSetRootTest
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from LesionNetworkMappingLib import controlsets  # noqa: E402
from LesionNetworkMappingLib.study import Study  # noqa: E402

try:
    import slicer

    HAS_APP = getattr(slicer, "app", None) is not None
except ImportError:
    HAS_APP = False


def _environWithout(name):
    env = dict(os.environ)
    env.pop(name, None)
    return env


class RegistryRootResolverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-controlset-root-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(controlsets.set_data_directory_resolver, controlsets._data_directory_resolver)

    def test_resolver_gives_the_registry_without_root_or_environment(self):
        controlsets.set_data_directory_resolver(lambda: self.tmp)
        with mock.patch.dict(os.environ, _environWithout(controlsets.ENV_ROOT), clear=True):
            self.assertEqual(controlsets.registry_root(), os.path.join(self.tmp, controlsets.REGISTRY_DIR))
            # An explicit root still wins
            self.assertEqual(controlsets.registry_root(os.path.join(self.tmp, "x")), os.path.join(self.tmp, "x"))
        # The environment variable (set by Library, inherited by jobs) still wins over the resolver
        with mock.patch.dict(os.environ, {controlsets.ENV_ROOT: os.path.join(self.tmp, "env")}):
            self.assertEqual(controlsets.registry_root(), os.path.join(self.tmp, "env"))

    def test_failing_or_empty_resolver_falls_back_to_the_default(self):
        default = os.path.join(controlsets.default_data_directory(), controlsets.REGISTRY_DIR)

        def broken():
            raise RuntimeError("no settings")

        with mock.patch.dict(os.environ, _environWithout(controlsets.ENV_ROOT), clear=True):
            for resolver in (broken, lambda: "", lambda: None, None):
                controlsets.set_data_directory_resolver(resolver)
                self.assertEqual(controlsets.registry_root(), default)


@unittest.skipUnless(HAS_APP, "needs the Slicer application")
class ApplicationRegistryRootTest(unittest.TestCase):
    """In the application, without Library's card, linked controls resolve in the data directory's registry."""

    SETTING = "Studio/LNM/DataDirectory"

    def setUp(self):
        import qt

        import LesionNetworkMapping  # noqa: F401  (the module is loaded at startup; its class installed the resolver)

        self.tmp = tempfile.mkdtemp(prefix="lnm-controlset-root-app-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        settings = qt.QSettings()
        previous = settings.value(self.SETTING)
        settings.setValue(self.SETTING, self.tmp)

        def restore():
            if previous is None:
                qt.QSettings().remove(self.SETTING)
            else:
                qt.QSettings().setValue(self.SETTING, previous)

        self.addCleanup(restore)
        self.addCleanup(setattr, Study, "controlset_root", Study.controlset_root)
        Study.controlset_root = None
        patcher = mock.patch.dict(os.environ, _environWithout(controlsets.ENV_ROOT), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_registry_is_the_data_directory_one(self):
        from LesionNetworkMappingUI import normalize_controller

        self.assertEqual(normalize_controller.dataDirectory(), self.tmp)
        self.assertEqual(controlsets.registry_root(), controlsets.root_for_data_directory(self.tmp))

    def test_linked_control_resolves_without_library(self):
        import numpy as np

        from LesionNetworkMappingLib import nifti
        from LesionNetworkMappingLib.geometry import GRID_BY_ID

        grid = GRID_BY_ID["NLin6-2mm"]
        folder = os.path.join(self.tmp, "controls")
        os.makedirs(folder)
        ijk = np.indices(grid.shape).reshape(3, -1).T
        world = ijk @ grid.affine[:3, :3].T + grid.affine[:3, 3]
        for n in range(2):
            inside = np.linalg.norm(world - np.asarray((-10.0 + 20 * n, 0.0, 0.0)), axis=1) <= 5
            nifti.write_nifti(os.path.join(folder, f"ctl_{n:03d}_lesion.nii.gz"), inside.reshape(grid.shape).astype(np.uint8), grid.affine)
        # Library registers and links at <DataDirectory>/ControlSets
        libraryRoot = controlsets.root_for_data_directory(self.tmp)
        controlsets.register_folder(folder, "Pool", root=libraryRoot, space="MNI152NLin6Asym")
        study = Study.create(os.path.join(self.tmp, "studies", "S"), name="S", space="MNI152NLin6Asym")
        study.link_control_set("Pool", root=libraryRoot)
        study.save()
        # After a restart, Map opens the study with nothing set by Library
        opened = Study.open(study.path)
        self.assertIsNone(opened.controlset_root)
        rows = controlsets.linked_rows(opened)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertTrue(os.path.isfile(opened.abspath(row["lesion"])), row["lesion"])

    def test_jobs_inherit_the_registry(self):
        from LesionNetworkMappingUI import jobrunner

        env = jobrunner.instance().environment(jobrunner.JobSpec("LesionNetworkMapping"), 1)
        self.assertEqual(env.get(controlsets.ENV_ROOT), controlsets.root_for_data_directory(self.tmp))


if __name__ == "__main__":
    unittest.main()

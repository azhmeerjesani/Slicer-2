"""Two connectomes with the same name, grid and subjects but different data have different identifiers.

A scene names its connectome by ``connectomeIdentifier`` (SPEC 9.7 rule 10). Two bundles named "GSP" made from different
time series (for example converted with and without global signal regression) once got one identifier, so a scene saved
with the second reopened with the first one registered. The identifier now includes the time series fingerprint that
connectome.json records (``subjectsNormsSha256``); bundles without it, and scenes of earlier versions, keep resolving.
It needs the Slicer application (no network)::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingSameIdentifierTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
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


class LesionNetworkMappingSameIdentifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from LesionNetworkMappingLib import connectome

        cls.tmp = tempfile.mkdtemp(prefix="lnm same id ")
        cls.noGsr, cls.gsr = (
            os.path.abspath(connectome.make_synthetic(os.path.join(cls.tmp, folder, "GSP.lnmc"), shape=(8, 9, 8), n_subjects=6, runs=(12, 12), seed=seed, name="GSP")["path"])
            for folder, seed in (("noGSR", 0), ("GSR", 7))
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        import qt
        from LesionNetworkMapping import CONNECTOMES_SETTING, LesionNetworkMappingLogic

        self.Logic = LesionNetworkMappingLogic
        self.savedSetting = qt.QSettings().value(CONNECTOMES_SETTING)
        self.savedKnown = dict(LesionNetworkMappingLogic._knownConnectomes)
        for path in (self.noGsr, self.gsr):  # the bundle without GSR is registered first
            LesionNetworkMappingLogic.registerConnectome(path)

    def tearDown(self):
        import qt
        from LesionNetworkMapping import CONNECTOMES_SETTING

        for path in (self.noGsr, self.gsr):
            self.Logic.unregisterConnectome(path)
        if self.savedSetting is not None:
            qt.QSettings().setValue(CONNECTOMES_SETTING, self.savedSetting)
        self.Logic._knownConnectomes.clear()
        self.Logic._knownConnectomes.update(self.savedKnown)

    def manifest(self, path):
        with open(os.path.join(path, "connectome.json"), encoding="utf-8") as f:
            return json.load(f)

    def test_data_different_bundles_have_their_own_identifier(self):
        from LesionNetworkMappingLib.connectome import NORMS_SHA256_KEY

        first, second = self.manifest(self.noGsr), self.manifest(self.gsr)
        self.assertEqual(first["subjects"], second["subjects"])
        self.assertNotEqual(first[NORMS_SHA256_KEY], second[NORMS_SHA256_KEY])

        logic = self.Logic()
        bundleA, bundleB = logic.openBundle(self.noGsr), logic.openBundle(self.gsr)
        idA, idB = logic.connectomeIdentifier(bundleA), logic.connectomeIdentifier(bundleB)
        self.assertNotEqual(idA, idB, "two connectomes with different data share one identifier")
        self.assertEqual(logic.connectomeIdentifierName(idB), "GSP")

        # A new session (nothing remembered): each identifier finds its own bundle, whatever the registration order
        self.Logic._knownConnectomes.clear()
        self.assertEqual(os.path.normpath(logic.resolveConnectome(idB)), os.path.normpath(self.gsr), "the scene reopens with the other connectome")
        self.assertEqual(os.path.normpath(logic.resolveConnectome(idA)), os.path.normpath(self.noGsr))

        # The same bundle in another folder keeps its identifier
        copy = os.path.join(self.tmp, "copy", "GSP.lnmc")
        shutil.copytree(self.gsr, copy)
        try:
            self.assertEqual(logic.connectomeIdentifier(logic.openBundle(copy)), idB)
        finally:
            shutil.rmtree(os.path.dirname(copy), ignore_errors=True)

    def test_identifiers_of_earlier_versions_still_resolve(self):
        logic = self.Logic()
        bundleB = logic.openBundle(self.gsr)
        earlier = logic.earlierConnectomeIdentifier(bundleB)
        self.assertNotEqual(earlier, logic.connectomeIdentifier(bundleB))
        self.Logic._knownConnectomes.clear()
        self.assertIn(logic.resolveConnectome(earlier), (self.noGsr, self.gsr), "a scene of an earlier version no longer finds its connectome")

    def test_bundle_without_norms_fingerprint_keeps_its_identifier(self):
        from LesionNetworkMappingLib.connectome import NORMS_SHA256_KEY
        from LesionNetworkMappingLib.export import data_sha256, identity_sha256

        metadata = self.manifest(self.gsr)
        metadata.pop(NORMS_SHA256_KEY)
        self.assertEqual(identity_sha256(metadata), data_sha256(metadata))


if __name__ == "__main__":
    unittest.main()

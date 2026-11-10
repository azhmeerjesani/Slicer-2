"""The Robust card's connectome split-half refuses an engine run made on another connectome (BB-1).

The summary is filed under the current connectome's folder (conn-<label>/robustness/connectome_splithalf) and cited in
that analysis's methods, so an engine run whose qa.json names another connectome must be refused with a reason, and the
summary must name its connectome. It needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingRobustConnSplitBundleTest

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


class _Bundle:
    def __init__(self, name):
        self.name = name


class _Runner:
    def isBusy(self):
        return False


class _Ctx:
    def __init__(self, bundle):
        self.study = object()
        self.bundle = bundle
        self.currentRun = None
        self.runner = _Runner()


class _Label:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text


def _card(engineRun, bundle, connDir):
    from LesionNetworkMappingUI import card_robust

    class Card:
        runState = card_robust.RobustCard.runState
        engineRunDir = card_robust.RobustCard.engineRunDir
        engineRunConnectome = card_robust.RobustCard.engineRunConnectome
        engineRunMismatch = card_robust.RobustCard.engineRunMismatch
        showConnectome = card_robust.RobustCard.showConnectome

        def __init__(self):
            self.ctx = _Ctx(bundle)
            self._engineRun = engineRun
            self.connLabel = _Label()

        def running(self):
            return False

        def connectomeDir(self):
            return connDir

    return Card()


class LesionNetworkMappingRobustConnSplitBundleTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lnm-connsplit-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.connDir = os.path.join(self.root, "study", "derivatives", "lnm", "conn-Synthetic")
        os.makedirs(self.connDir)

    def engineRun(self, name, bundle):
        folder = os.path.join(self.root, name)
        os.makedirs(folder)
        with open(os.path.join(folder, "qa.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-lnm-qa", "version": 1, "bundle": bundle, "n": 12, "lesions": {},
                       "split_half": {"label": "x", "n_odd": 6, "n_even": 6, "r": {"sub-01": 0.979}}}, f)
        return folder

    def test_runOnAnotherConnectomeIsRefused(self):
        card = _card(self.engineRun("engine-split-second", "Second connectome"), _Bundle("Synthetic"), self.connDir)
        enabled, reason = card.runState("connsplit")
        self.assertFalse(enabled)
        self.assertIn("Second connectome", reason)
        self.assertIn("Synthetic", reason)

    def test_runOnAnotherConnectomeIsRefusedWithoutBundle(self):
        # No connectome chosen: the folder the summary would be filed under decides
        card = _card(self.engineRun("engine-split-second", "Second connectome"), None, self.connDir)
        enabled, reason = card.runState("connsplit")
        self.assertFalse(enabled)
        self.assertIn("Second connectome", reason)

    def test_runOnThisConnectomeIsAccepted(self):
        card = _card(self.engineRun("engine-split", "Synthetic"), _Bundle("Synthetic"), self.connDir)
        self.assertEqual(card.runState("connsplit"), (True, ""))
        previewDir = os.path.join(os.path.dirname(self.connDir), "conn-Synthetic_n-10")
        self.assertEqual(_card(card._engineRun, None, previewDir).runState("connsplit"), (True, ""))

    def test_summaryNamesItsConnectome(self):
        from LesionNetworkMappingLib import methods, robustness

        folder = self.engineRun("engine-split", "Synthetic")
        result = robustness.connectome_split_half(folder)
        self.assertEqual(result["bundle"], "Synthetic")
        summary = {"split_half": {key: result[key] for key in ("label", "bundle", "n_odd", "n_even", "median_r", "min_r", "small_halves")}}
        self.assertIn("connectome split-half of Synthetic", methods._connectome_split_half_text(summary, {}, None))
        out = os.path.join(self.root, "connectome_splithalf")
        os.makedirs(out)
        with open(os.path.join(out, "connectome_split_half.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in result.items() if k != "rows"}, f)
        card = _card(folder, _Bundle("Synthetic"), self.connDir)
        card.showConnectome(out)
        self.assertIn("of Synthetic", card.connLabel.text)

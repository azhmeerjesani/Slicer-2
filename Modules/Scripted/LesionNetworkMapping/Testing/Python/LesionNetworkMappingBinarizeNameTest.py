"""Test of the name of the volume that Binarize makes (bug map-single:binarize-volume-name-rounds-threshold).

Binarize named the new 0/1 volume with the threshold rounded to two decimals ("mask ±Fz≥0.00" for |Fz| >= 0.004, "≥0.12" for
0.125), which differs from the threshold applied and from the one the legend, the derived line and the sidecars state. The name now
uses ``thresholds.format_value`` like they do.

Needs the Slicer application::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingBinarizeNameTest

Under a plain PythonSlicer (the library test glob) the module is skipped.
"""

import types
import unittest

try:
    import slicer
except ImportError as exc:
    raise unittest.SkipTest("needs the Slicer application") from exc
if getattr(slicer, "app", None) is None:
    raise unittest.SkipTest("needs the Slicer application")


class BinarizeNameTest(unittest.TestCase):
    def setUp(self):
        slicer.mrmlScene.Clear(0)
        slicer.util.selectModule("LesionNetworkMapping")

    def tearDown(self):
        slicer.mrmlScene.Clear(0)

    def _binarize(self, statistic, rule, value, sign="both", values=(0.0, 0.003, 0.004, 0.2)):
        """The volume that Binarize makes of a small map under ``rule`` resolved to ``value`` (widget without its GUI)."""
        import numpy as np

        from LesionNetworkMapping import LesionNetworkMappingWidget
        from LesionNetworkMappingLib import thresholds

        volume = slicer.util.addVolumeFromArray(np.array(values, dtype=np.float32).reshape(1, 1, -1), name="Map")
        widget = object.__new__(LesionNetworkMappingWidget)
        widget._parameterNode = types.SimpleNamespace(resultVolume=volume, sign=sign, statistic=statistic)
        widget._ruleAppliesToShownMap = lambda: True
        widget.resolved = thresholds.Resolved(statistic=statistic, value=value, df=119, p_unc=None, p_fwe=None, banner=None, allowed=True, n=120, v=4, rule=rule)
        return widget.onBinarize(), thresholds.format_value(widget.resolved)

    def test_name_states_the_threshold_as_the_legend_does(self):
        cases = (
            ("Fz", {"kind": "fz_abs", "value": 0.004}, 0.004, "Fz≥0.004"),
            ("Fz", {"kind": "fz_abs", "value": 0.125}, 0.125, "Fz≥0.125"),
            ("Fz", {"kind": "fz_abs", "value": 0.999}, 0.999, "Fz≥0.999"),
            ("Fz", {"kind": "top_pct", "value": 7.0}, 0.0381234, "Fz≥0.0381"),
            ("T", {"kind": "t_abs", "value": 5.2749}, 5.2749, "T≥5.27"),
        )
        for statistic, rule, value, expected in cases:
            with self.subTest(expected=expected):
                binary, shown = self._binarize(statistic, rule, value)
                self.assertIsNotNone(binary)
                self.assertTrue(binary.GetName().endswith(expected), binary.GetName())
                self.assertTrue(binary.GetName().endswith("≥" + shown), binary.GetName())

    def test_voxels_are_unchanged(self):
        import numpy as np

        binary, _shown = self._binarize("Fz", {"kind": "fz_abs", "value": 0.004}, 0.004)
        self.assertEqual(slicer.util.arrayFromVolume(binary).ravel().tolist(), [0, 0, 1, 1])
        self.assertTrue(np.issubdtype(slicer.util.arrayFromVolume(binary).dtype, np.integer))


if __name__ == "__main__":
    unittest.main()

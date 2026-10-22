"""Unit tests of threshold rules v2 (top-x%, % of in-mask voxels, GSR banner) and the threshold sweep.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingThresholdsTest.py -v
"""

import json
import math
import os
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import robustness, thresholds
from LesionNetworkMappingLib.geometry import Grid


class UnknownNRuleTest(unittest.TestCase):
    """A map whose N is unknown (a T map file without a sidecar): only the rules that do not need N resolve (W3-I1)."""

    def setUp(self):
        self.values = np.linspace(-20.0, 26.0, 1001)

    def test_custom_t_as_typed(self):
        resolved = thresholds.resolve_unknown_n({"kind": "custom_t", "value": 14.62}, len(self.values), values=self.values)
        self.assertEqual((resolved.statistic, resolved.value, resolved.n, resolved.df), ("T", 14.62, 0, 0))
        self.assertTrue(resolved.allowed)
        self.assertIsNone(resolved.banner)
        self.assertIsNone(resolved.p_unc)
        self.assertIn(thresholds.N_UNKNOWN_NOTE, resolved.notes)
        self.assertAlmostEqual(resolved.fraction, float((np.abs(self.values) >= 14.62).mean()))
        line = thresholds.describe(resolved, resolved.fraction)
        self.assertIn("|T| ≥ 14.62 at N unknown", line)
        self.assertIn("no p-value", line)
        # Round trip through JSON (provenance)
        self.assertEqual(thresholds.Resolved.from_json(resolved.to_json()), resolved)

    def test_custom_t_with_reference_n_has_banner(self):
        resolved = thresholds.resolve_unknown_n({"kind": "custom_t", "value": 5.0, "reference_n": 120}, 100)
        self.assertTrue(resolved.allowed)
        self.assertIn("N≈120", resolved.banner)

    def test_n_free_rules(self):
        fz = thresholds.resolve_unknown_n({"kind": "fz_abs", "value": 0.1}, 100)
        self.assertTrue(fz.allowed)
        self.assertIn("at N unknown", thresholds.describe(fz))
        top = thresholds.resolve_unknown_n({"kind": "top_pct", "value": 10, "statistic": "T"}, len(self.values), values=self.values)
        self.assertTrue(top.allowed)
        self.assertAlmostEqual(top.fraction, 0.1, delta=0.002)
        self.assertIn("at N unknown", thresholds.describe(top))

    def test_rules_that_need_n(self):
        for rule in ({"kind": "fwe_bonferroni", "alpha": 0.05}, {"kind": "t_convention"}):
            with self.subTest(rule=rule["kind"]), self.assertRaisesRegex(ValueError, "needs the number of subjects"):
                thresholds.resolve_unknown_n(rule, 100)

    def test_known_n_unchanged(self):
        resolved = thresholds.resolve({"kind": "fz_abs", "value": 0.1}, 0, 100)
        self.assertIn("at N=0", thresholds.describe(resolved))


class TopPercentRuleTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(7)
        self.values = rng.standard_normal(10_001).astype(np.float32) * 3.0
        self.values[:5] = np.nan  # non-finite values are ignored
        self.values[5] = np.inf

    def test_normalize(self):
        self.assertEqual(thresholds.normalize_rule({"kind": "top_pct"}), {"kind": "top_pct", "value": 10.0, "statistic": "Fz", "sign": "both"})
        self.assertEqual(thresholds.rule_label({"kind": "top_pct"}), "Top x% (N-independent)")
        for bad in (
            {"kind": "top_pct", "value": 0},
            {"kind": "top_pct", "value": 100},
            {"kind": "top_pct", "value": -5},
            {"kind": "top_pct", "value": None},
            {"kind": "top_pct", "statistic": "Z"},
            {"kind": "top_pct", "sign": "up"},
        ):
            with self.subTest(rule=bad), self.assertRaises(ValueError):
                thresholds.normalize_rule(bad)
        rule = {"kind": "top_pct", "value": 25, "statistic": "T", "sign": "neg"}
        self.assertEqual(thresholds.rule_from_json(thresholds.rule_to_json(rule)), thresholds.normalize_rule(rule))

    def test_resolves_to_the_percentile_and_keeps_x_percent(self):
        finite = self.values[np.isfinite(self.values)].astype(np.float64)
        for statistic in ("Fz", "T"):
            for sign, scores in (("both", np.abs(finite)), ("pos", finite), ("neg", -finite)):
                for x in (1.0, 5.0, 10.0, 25.0, 50.0):
                    with self.subTest(statistic=statistic, sign=sign, x=x):
                        rule = {"kind": "top_pct", "value": x, "statistic": statistic, "sign": sign}
                        resolved = thresholds.resolve(rule, 312, finite.size, values=self.values)
                        self.assertEqual(resolved.statistic, statistic)
                        self.assertEqual(resolved.value, float(np.percentile(scores, 100.0 - x)))
                        self.assertIsNone(resolved.p_unc)
                        self.assertIsNone(resolved.p_fwe)
                        self.assertTrue(resolved.allowed)
                        self.assertIsNone(resolved.banner)
                        self.assertEqual(resolved.kind, "top_pct")
                        kept = int((scores >= resolved.value).sum())
                        self.assertLessEqual(abs(kept - x / 100.0 * finite.size), 1.0)
                        self.assertAlmostEqual(resolved.fraction, kept / finite.size)

    def test_n_independent(self):
        rule = {"kind": "top_pct", "value": 10}
        self.assertEqual(thresholds.resolve(rule, 50, 1000, values=self.values).value, thresholds.resolve(rule, 1000, 1000, values=self.values).value)

    def test_nan_without_values(self):
        resolved = thresholds.resolve({"kind": "top_pct"}, 1000, 292_019)
        self.assertTrue(math.isnan(resolved.value))
        self.assertFalse(resolved.allowed)
        self.assertEqual(resolved.banner, "The top-x% rule needs the map values.")
        self.assertIsNone(resolved.p_unc)
        self.assertIsNone(resolved.fraction)
        self.assertIsNone(resolved.to_dict()["value"])
        self.assertEqual(thresholds.describe(resolved), "⇒ top 10% of |Fz| at N=1000, V=292,019 · rank rule (no p-value)")
        empty = thresholds.resolve({"kind": "top_pct"}, 1000, 10, values=[np.nan, np.inf])
        self.assertTrue(math.isnan(empty.value))
        self.assertFalse(empty.allowed)

    def test_no_fz_equivalents(self):
        # No API converts a T rule into an Fz value (or back): T rules resolve to T, fz_abs to Fz
        names = [name for name in dir(thresholds) if "equiv" in name.lower() or ("fz" in name.lower() and "t" in name.lower().replace("fz", "") and "to" in name.lower())]
        self.assertEqual(names, [])
        for kind in ("fwe_bonferroni", "t_convention", "custom_t"):
            resolved = thresholds.resolve({"kind": kind}, 1000, 292_019, values=self.values)
            self.assertEqual(resolved.statistic, "T")
            self.assertNotIn("Fz", thresholds.describe(resolved, fraction=resolved.fraction))


class GsrAndReadoutTest(unittest.TestCase):
    def test_gsr_banner_only_for_symmetric_t_rules_without_gsr(self):
        banner = thresholds.GSR_BANNER
        self.assertEqual(banner, "Non-GSR connectomes centre uninformative connections above 0; consider asymmetric or effect-size thresholds (Cohen & Fox 2021).")
        values = np.linspace(-8, 8, 101)
        rules = {
            "fwe_bonferroni": {"kind": "fwe_bonferroni"},
            "t_convention": {"kind": "t_convention"},
            "custom_t": {"kind": "custom_t", "value": 5},
            "fz_abs": {"kind": "fz_abs", "value": 0.05},
            "top_pct": {"kind": "top_pct", "value": 10, "statistic": "T"},
        }
        for kind, rule in rules.items():
            for gsr in (None, True, False):
                with self.subTest(kind=kind, gsr=gsr):
                    resolved = thresholds.resolve(rule, 1000, 292_019, gsr=gsr, values=values)
                    has_banner = resolved.banner is not None and banner in resolved.banner
                    self.assertEqual(has_banner, gsr is False and kind in ("fwe_bonferroni", "t_convention", "custom_t"))
        # Concatenated after an existing banner
        both = thresholds.resolve({"kind": "t_convention"}, 312, 292_019, note="partial", gsr=False)
        self.assertEqual(both.banner, f"This threshold was defined for N≈1000; this map has N=312 (partial). Use FWE or effect size. {banner}")
        self.assertFalse(both.allowed)

    def test_negative_peaks_note(self):
        note = "The magnitude and extent of negative correlations are influenced by global signal regression (Murphy & Fox 2017)."
        self.assertEqual(thresholds.negative_peaks_note(True), note)
        self.assertIsNone(thresholds.negative_peaks_note(False))
        self.assertIsNone(thresholds.negative_peaks_note(None))
        self.assertEqual(thresholds.resolve(None, 1000, 292_019, gsr=True).notes, (note,))
        self.assertEqual(thresholds.resolve(None, 1000, 292_019, gsr=False).notes, ())
        self.assertEqual(thresholds.resolve({"kind": "top_pct", "sign": "pos"}, 1000, 3, gsr=True, values=[1, 2, 3]).notes, ())

    def test_fraction_connected(self):
        values = np.array([-3.0, -1.0, 0.0, 0.5, 2.0, 4.0, np.nan, np.inf], dtype=np.float32)
        resolved = thresholds.resolve({"kind": "custom_t", "value": 2.0}, 100, 6)
        self.assertAlmostEqual(thresholds.fraction_connected(values, resolved), 3 / 6)
        self.assertAlmostEqual(thresholds.fraction_connected(values, resolved, sign="pos"), 2 / 6)
        self.assertAlmostEqual(thresholds.fraction_connected(values, resolved, sign="neg"), 1 / 6)
        self.assertTrue(math.isnan(thresholds.fraction_connected([np.nan], resolved)))
        self.assertTrue(math.isnan(thresholds.fraction_connected(values, thresholds.resolve({"kind": "top_pct"}, 100, 6))))
        self.assertEqual(thresholds.fraction_connected(values, thresholds.resolve(None, 1, 6)), 0.0)
        with self.assertRaises(ValueError):
            thresholds.fraction_connected(values, resolved, sign="up")
        # resolve() fills the fraction when the values are given
        self.assertAlmostEqual(thresholds.resolve({"kind": "custom_t", "value": 2.0}, 100, 6, values=values).fraction, 0.5)
        # The rule's sign is the default
        top = thresholds.resolve({"kind": "top_pct", "value": 50, "sign": "pos"}, 100, 6, values=values)
        self.assertAlmostEqual(thresholds.fraction_connected(values, top), top.fraction)

    def test_describe_with_fraction(self):
        fwe = thresholds.resolve({"kind": "fwe_bonferroni"}, 1000, 292_019)
        self.assertEqual(thresholds.describe(fwe), "⇒ |T| ≥ 5.27 at N=1000 (df=999), V=292,019 · p_unc ≤ 1.7e-7")
        self.assertEqual(thresholds.describe(fwe, fraction=0.634), "⇒ |T| ≥ 5.27 at N=1000 (df=999), V=292,019 · p_unc ≤ 1.7e-7 · 63% of in-mask voxels")
        self.assertTrue(thresholds.describe(fwe, fraction=0.0123).endswith(" · 1.2% of in-mask voxels"))
        self.assertTrue(thresholds.describe(fwe, fraction=0.00034).endswith(" · 0.034% of in-mask voxels"))
        self.assertTrue(thresholds.describe(fwe, fraction=0.0).endswith(" · 0% of in-mask voxels"))
        self.assertEqual(thresholds.describe(fwe, fraction=math.nan), thresholds.describe(fwe))
        fz = thresholds.resolve({"kind": "fz_abs", "value": 0.05}, 312, 292_019)
        self.assertEqual(thresholds.describe(fz, fraction=0.25), "⇒ |Fz| ≥ 0.05 at N=312, V=292,019 · effect size (no p-value) · 25% of in-mask voxels")
        top = thresholds.resolve({"kind": "top_pct", "value": 10, "statistic": "T", "sign": "pos"}, 312, 4, values=[1.0, 2.0, 3.0, 4.0])
        self.assertEqual(thresholds.describe(top, fraction=top.fraction), "⇒ top 10% of T (≥ 3.7) at N=312, V=4 · rank rule (no p-value) · 25% of in-mask voxels")


class ResolvedCompatibilityTest(unittest.TestCase):
    def test_from_dict_on_an_old_dict(self):
        # A resolved threshold as written by the first version (no kind, fraction or notes)
        old = {
            "statistic": "T",
            "value": 5.27,
            "df": 999,
            "p_unc": 1.7e-7,
            "p_fwe": 0.05,
            "banner": None,
            "allowed": True,
            "n": 1000,
            "v": 292019,
            "rule": {"kind": "fwe_bonferroni", "alpha": 0.05},
        }
        resolved = thresholds.Resolved.from_dict(old)
        self.assertIsNone(resolved.kind)
        self.assertIsNone(resolved.fraction)
        self.assertEqual(resolved.notes, ())
        self.assertEqual(resolved.value, 5.27)
        self.assertTrue(thresholds.describe(resolved).startswith("⇒ |T| ≥ 5.27"))
        # Old readers build Resolved from the fields they know; the new keys are extra keys
        self.assertEqual({key: value for key, value in resolved.to_dict().items() if key in old}, old)

    def test_round_trip_with_new_fields(self):
        values = np.linspace(-1, 1, 1001)
        for rule, gsr in (({"kind": "top_pct", "value": 25, "statistic": "Fz"}, True), ({"kind": "custom_t", "value": 6}, False), (None, True)):
            with self.subTest(rule=rule):
                resolved = thresholds.resolve(rule, 640, 1001, gsr=gsr, values=values)
                data = json.loads(resolved.to_json())
                self.assertIsInstance(data["notes"], list)
                self.assertEqual(thresholds.Resolved.from_dict(data), resolved)
        for bad in ({"notes": "x"}, {"notes": 3}, {"fraction": "half"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                thresholds.Resolved.from_dict({**thresholds.resolve(None, 640, 1000).to_dict(), **bad})


def _brute_force_cell(maps, rule, n, v, overlap_pct, sign="both"):
    """Overlap % map and binary map of one sweep cell, voxel by voxel (reference implementation)."""
    n_lesions, n_vox = maps.shape
    rule = thresholds.normalize_rule(rule)
    if rule["kind"] != "top_pct":
        cut = thresholds.resolve(rule, n, v).value
    overlap = np.zeros(n_vox)
    for lesion in range(n_lesions):
        row = maps[lesion].astype(np.float64)
        rule_sign = rule["sign"] if rule["kind"] == "top_pct" else sign
        if rule["kind"] == "top_pct":
            finite = row[np.isfinite(row)]
            scores = {"both": np.abs(finite), "pos": finite, "neg": -finite}[rule_sign]
            cut = np.percentile(scores, 100.0 - rule["value"])
        for voxel in range(n_vox):
            value = row[voxel]
            score = {"both": abs(value), "pos": value, "neg": -value}[rule_sign]
            if math.isfinite(value) and score >= cut:
                overlap[voxel] += 100.0 / n_lesions
    count = np.round(overlap * n_lesions / 100.0)
    binary = count * 100.0 >= overlap_pct * n_lesions - 1e-9
    return overlap, binary


class ThresholdSweepTest(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(11)
        self.grid = Grid((6, 5, 4), np.diag([2.0, 2.0, 2.0, 1.0]))
        self.n_lesions = 7
        v = self.grid.n_voxels
        # A shared "network" in one corner plus noise, as T maps
        signal = np.zeros(v)
        signal[:12] = 9.0
        signal[12:20] = -6.0
        self.maps = (signal + rng.standard_normal((self.n_lesions, v)) * 3.0).astype(np.float32)
        self.maps[0, 50] = np.nan

    def test_cells_equal_brute_force(self):
        n, v = 1000, self.maps.shape[1]
        primary = {"kind": "custom_t", "value": 7.0, "reference_n": 1000}
        roi = np.zeros(v, dtype=bool)
        roi[100:110] = True
        for sign in ("both", "pos"):
            rows = robustness.threshold_sweep(self.maps, n, v, primary, 75, sign=sign, grid=self.grid, roi=roi)
            primary_overlap, primary_binary = _brute_force_cell(self.maps, primary, n, v, 75, sign)
            self.assertTrue(rows[0]["is_primary"])
            self.assertEqual(rows[0]["dice_vs_primary"], 1.0)
            cells = [row for row in rows if not row["is_primary"]]
            self.assertEqual(len(cells), 8 * len(robustness.DEFAULT_OVERLAPS))  # FWE, 4 T, 3 top-x%
            for row in cells:
                with self.subTest(sign=sign, setting=row["rule_label"], overlap=row["overlap_pct"]):
                    rule = row["rule"]
                    if rule["kind"] == "top_pct":
                        self.assertEqual(rule["sign"], sign)
                    overlap, binary = _brute_force_cell(self.maps, rule, n, v, row["overlap_pct"], sign)
                    self.assertEqual(row["voxels"], int(binary.sum()))
                    self.assertAlmostEqual(row["ml"], binary.sum() * 0.008)
                    total = binary.sum() + primary_binary.sum()
                    dice = 2.0 * (binary & primary_binary).sum() / total if total else math.nan
                    if math.isnan(dice):
                        self.assertTrue(math.isnan(row["dice_vs_primary"]))
                    else:
                        self.assertAlmostEqual(row["dice_vs_primary"], dice)
                    self.assertAlmostEqual(row["r_vs_primary"], float(np.corrcoef(overlap, primary_overlap)[0, 1]), places=9)
                    self.assertEqual(row["roi_survives"], bool((binary & roi).any()))
                    self.assertIsInstance(row["peak_persists"], bool)

    def test_peak_persistence(self):
        v = self.maps.shape[1]
        world = self.grid.voxel_to_world(np.stack(np.unravel_index(np.arange(v), self.grid.shape), axis=1))
        peak = world[3]
        rows = robustness.threshold_sweep(self.maps, 1000, v, {"kind": "fwe_bonferroni"}, 50, grid=self.grid, peak_world=peak)
        near = np.linalg.norm(world - peak, axis=1) <= 10.0
        for row in rows:
            if row["enabled"]:
                _, binary = _brute_force_cell(self.maps, row["rule"], 1000, v, row["overlap_pct"])
                self.assertEqual(row["peak_persists"], bool((binary & near).any()))
        # Without a grid nothing can be said about the peak; mask indices select the grid voxels
        self.assertIsNone(robustness.threshold_sweep(self.maps, 1000, v, None, 50)[1]["peak_persists"])
        subset = np.arange(0, v, 2)
        rows = robustness.threshold_sweep(self.maps[:, subset], 1000, subset.size, None, 50, grid=self.grid, mask_indices=subset)
        self.assertIsInstance(rows[0]["peak_persists"], bool)
        with self.assertRaises(ValueError):
            robustness.threshold_sweep(self.maps[:, subset], 1000, subset.size, None, 50, grid=self.grid)

    def test_t_rules_disabled_at_n312(self):
        entries = robustness.DEFAULT_RULES(312)
        self.assertEqual([entry["label"] for entry in entries], ["FWE (Bonferroni)", "|T| ≥ 5 (N≈1000)", "|T| ≥ 7 (N≈1000)", "|T| ≥ 9 (N≈1000)", "|T| ≥ 12 (N≈1000)", "Top 5%", "Top 10%", "Top 25%"])
        disabled = [entry for entry in entries if not entry["enabled"]]
        self.assertEqual([entry["rule"]["value"] for entry in disabled], [5.0, 7.0, 9.0, 12.0])
        for entry in disabled:
            self.assertIn("needs N ≥ 900; this map has N=312", entry["reason"])
        self.assertTrue(all(entry["enabled"] for entry in robustness.DEFAULT_RULES(1000)))
        rows = robustness.threshold_sweep(self.maps, 312, self.maps.shape[1], None, 75)
        enabled_labels = {row["rule_label"] for row in rows if row["enabled"] and not row["is_primary"]}
        self.assertEqual(enabled_labels, {"FWE (Bonferroni)", "Top 5%", "Top 10%", "Top 25%"})
        disabled_rows = [row for row in rows if not row["enabled"]]
        self.assertEqual(len(disabled_rows), 4)
        self.assertTrue(all(row["voxels"] is None and row["reason"] for row in disabled_rows))
        # Fz maps: only the top-x% rules apply
        fz_entries = robustness.DEFAULT_RULES(1000, statistic="Fz")
        self.assertEqual([entry["label"] for entry in fz_entries if entry["enabled"]], ["Top 5%", "Top 10%", "Top 25%"])
        self.assertTrue(all(entry["rule"]["statistic"] == "Fz" for entry in fz_entries if entry["rule"]["kind"] == "top_pct"))

    def test_conclusion_flips(self):
        def row(label, overlap, roi=None, peak=None, primary=False):
            return {"rule_label": label, "overlap_pct": overlap, "roi_survives": roi, "peak_persists": peak, "is_primary": primary, "enabled": True}

        rows = [
            row("Primary: FWE (Bonferroni)", 75, roi=True, peak=True, primary=True),
            row("FWE (Bonferroni)", 50, roi=True, peak=True),
            row("FWE (Bonferroni)", 90, roi=True, peak=True),
            row("Top 5%", 50, roi=False, peak=True),
            row("Top 5%", 90, roi=False, peak=False),
        ]
        flip = robustness.conclusion_flips(rows)
        self.assertEqual((flip["rule_label"], flip["overlap_pct"], flip["lost"]), ("Top 5%", 50, ["ROI"]))
        self.assertEqual(flip["text"], "Conclusion flips at Top 5%, ≥ 50% overlap (the ROI no longer survives).")
        self.assertIsNone(robustness.conclusion_flips(rows[:3]))
        # A criterion that fails for the primary cannot flip
        rows[0]["roi_survives"] = False
        self.assertEqual(robustness.conclusion_flips(rows)["lost"], ["peak"])
        # Disabled rows are skipped
        self.assertIsNone(robustness.conclusion_flips([rows[0], {**rows[4], "enabled": False}]))
        # On a real sweep: an ROI in pure noise is lost as soon as the setting gets strict enough
        v = self.maps.shape[1]
        roi = np.zeros(v, dtype=bool)
        roi[:12] = True
        sweep = robustness.threshold_sweep(self.maps, 1000, v, {"kind": "custom_t", "value": 5.0}, 50, roi=roi)
        flip = robustness.conclusion_flips(sweep)
        first_lost = next(r for r in sweep[1:] if r["enabled"] and r["roi_survives"] is False)
        self.assertEqual((flip["rule_label"], flip["overlap_pct"]), (first_lost["rule_label"], first_lost["overlap_pct"]))

    def test_no_best_setting_and_tsv(self):
        rows = robustness.threshold_sweep(self.maps, 312, self.maps.shape[1], {"kind": "top_pct", "value": 10, "statistic": "T"}, 75, grid=self.grid)
        self.assertEqual(sum(row["is_primary"] for row in rows), 1)
        self.assertTrue(all("best" not in key for row in rows for key in row))
        self.assertFalse([name for name in dir(robustness) if "best" in name.lower()])
        with tempfile.TemporaryDirectory() as tmp:
            path = robustness.write_sweep_tsv(rows, os.path.join(tmp, "sweep.tsv"))
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        self.assertEqual(lines[0].split("\t"), list(robustness.TSV_COLUMNS))
        self.assertEqual(len(lines), len(rows) + 1)
        self.assertTrue(all(len(line.split("\t")) == len(robustness.TSV_COLUMNS) for line in lines))
        first = dict(zip(robustness.TSV_COLUMNS, lines[1].split("\t"), strict=True))
        self.assertEqual(first["setting"], "Primary: Top 10%")
        self.assertEqual(first["primary"], "true")
        self.assertTrue(first["threshold"].startswith("per map T "))
        last = dict(zip(robustness.TSV_COLUMNS, lines[-1].split("\t"), strict=True))
        self.assertEqual((last["voxels"], last["threshold"]), ("n/a", "n/a"))
        self.assertIn("needs N ≥ 900", last["note"])

    def test_invalid_arguments(self):
        v = self.maps.shape[1]
        for kwargs in ({"maps": self.maps[0]}, {"primary_overlap": 0}, {"overlaps": [120]}, {"sign": "up"}):
            arguments = {"maps": self.maps, "n": 1000, "v": v, "primary_rule": None, "primary_overlap": 75, **kwargs}
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(ValueError):
                robustness.threshold_sweep(**arguments)


if __name__ == "__main__":
    unittest.main()

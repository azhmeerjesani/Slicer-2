"""Unit tests of analysis recipes, the analysis-plan lock with its deviations log, and a-priori ROI inference (L-39).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingRecipesTest.py -v
"""

import contextlib
import glob
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import design as designlib
from LesionNetworkMappingLib import geometry, glm, methods, nifti, recipes, roi
from LesionNetworkMappingLib.study import Study

BUILTIN_IDS = (
    "quicklook_boes2015",
    "classic_ferguson2019_adapted",
    "vdh2026_standard_settings",
    "petersen2026_label_permutation",
    "siddiqi2021_slnm",
    "darby2019_cnm",
    "comprehensive_battery",
    "preview",
)


def _env():
    return dict(os.environ, PYTHONPATH=MODULE_DIR + os.pathsep + os.environ.get("PYTHONPATH", ""))


class _TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-recipes-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def make_study(self, name="study", rows=()):
        study = Study.create(self.path(name), name=name)
        for sid, values in rows:
            study.add_subject(sid, values)
        study.save()
        return study

    def builtin(self, recipe_id):
        return recipes.find_recipe(recipe_id, recipes.load_recipes())


class BuiltinRecipesTest(_TempCase):
    def test_schema_of_every_builtin(self):
        files = sorted(glob.glob(os.path.join(recipes.BUILTIN_DIR, "*.json")))
        self.assertEqual(sorted(os.path.splitext(os.path.basename(path))[0] for path in files), sorted(BUILTIN_IDS))
        for path in files:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            with self.subTest(recipe=os.path.basename(path)):
                self.assertEqual(recipes.validate_recipe(data, builtin=True), [])
                self.assertEqual(data["id"], os.path.splitext(os.path.basename(path))[0])
                self.assertIn(data["status"], recipes.STATUSES)
                self.assertTrue(data["citations"])
                needed = {module for step in data["steps"] for module in recipes.STEPS[step][1]}
                self.assertEqual(set(data["requires"]), needed)
                self.assertNotIn(os.sep + "home" + os.sep, json.dumps(data))  # no local path in a shared file
        loaded = recipes.load_recipes()
        self.assertEqual({recipe.id for recipe in loaded}, set(BUILTIN_IDS))
        self.assertTrue(all(recipe.origin == "builtin" for recipe in loaded))

    def test_no_recipe_is_recommended(self):
        for recipe in recipes.load_recipes():
            for text in (recipe.title, recipe.get("description"), recipe.status_text()):
                self.assertNotIn("recommended", str(text).lower())
        self.assertEqual(self.builtin("comprehensive_battery").title, "Comprehensive diagnostics battery")
        data = dict(self.builtin("preview").data, title="Recommended pipeline")
        self.assertTrue(any("recommended" in problem for problem in recipes.validate_recipe(data)))

    def test_ferguson_is_adapted_with_differences(self):
        recipe = self.builtin("classic_ferguson2019_adapted")
        self.assertEqual(recipe.status, "adapted")
        self.assertEqual(recipe.status_text(), "adapted from Ferguson et al. 2019")
        differences = " ".join(recipe["differences_from_source"])
        for phrase in ("SPM12", "parametric two-sample", "permutation GLM", "lesion-volume covariate"):
            self.assertIn(phrase, differences)
        # An adapted recipe without differences is refused
        data = dict(recipe.data, differences_from_source=[])
        self.assertTrue(any("differences" in problem for problem in recipes.validate_recipe(data, builtin=True)))

    def test_schema_rejects_bad_content(self):
        good = self.builtin("preview").data
        cases = {
            "status": dict(good, status="best"),
            "steps": dict(good, steps=["maps", "teleport"]),
            "requires": dict(good, requires=["cohort"]),
            "citations": dict(good, citations=["not a doi"]),
            "parameters": dict(good, parameters={"n_perm": 10}),
            "nulls": dict(good, steps=["maps", "nulls"], requires=["cohortmaps", "nulls"]),
            "volume": dict(good, parameters={"design": {"covariates": []}}),
        }
        for name, data in cases.items():
            with self.subTest(case=name):
                self.assertTrue(recipes.validate_recipe(data, builtin=True))
        self.assertTrue(recipes.validate_recipe(dict(good, status="lab"), builtin=True))  # lab only outside the built-ins
        self.assertEqual(recipes.validate_recipe(dict(good, status="lab")), [])


class StepsTest(_TempCase):
    def test_partial_badge_with_missing_module(self):
        recipe = self.builtin("classic_ferguson2019_adapted")
        steps = recipes.available_steps(recipe, module_available=lambda name: name != "glm")
        by_id = {step["id"]: step for step in steps}
        self.assertEqual([step["id"] for step in steps], recipe.steps)  # never skipped, in order
        self.assertFalse(by_id["specificity"]["available"])
        self.assertEqual(by_id["specificity"]["status"], recipes.NOT_AVAILABLE)
        self.assertEqual(by_id["specificity"]["missing"], ["glm"])
        self.assertTrue(by_id["maps"]["available"])
        self.assertTrue(recipes.is_partial(recipe, lambda name: name != "glm"))
        self.assertEqual(recipes.partial_badge(recipe, lambda name: name != "glm"), "1 step not available in this build: Specificity (permutation GLM)")
        self.assertEqual(recipes.partial_badge(recipe, lambda name: True), "")
        # The real lookup (importlib.util.find_spec) finds this build's modules
        self.assertTrue(all(step["available"] for step in recipes.available_steps(self.builtin("preview"))))
        missing = recipes.available_steps(self.builtin("darby2019_cnm"), module_available=lambda name: name != "coords")
        self.assertEqual([step["id"] for step in missing if not step["available"]], ["coordinates"])

    def test_pipeline_config(self):
        from LesionNetworkMappingLib import pipeline

        recipe = self.builtin("classic_ferguson2019_adapted")
        result = recipes.to_pipeline_config(recipe, module_available=lambda name: True)
        config = pipeline.PipelineConfig.from_json(result["config"])
        self.assertEqual((config.nulls, config.specificity, config.scores, config.perm, config.input), ("off", "auto", "none", 5000, "AvgFz"))
        self.assertEqual(config.rule, {"kind": "fwe_bonferroni", "alpha": 0.05})
        self.assertEqual(result["recipe"]["id"], recipe.id)
        self.assertEqual(result["unavailable"], [])
        vdh = recipes.to_pipeline_config(self.builtin("vdh2026_standard_settings"), module_available=lambda name: name != "robustness")
        self.assertEqual(vdh["config"]["nulls"], "exact")
        self.assertEqual(vdh["unavailable"], ["robustness"])
        self.assertEqual([step["id"] for step in vdh["outside_pipeline"]], ["robustness"])
        preview = pipeline.PipelineConfig.from_json(recipes.to_pipeline_config(self.builtin("preview"))["config"])
        self.assertEqual(preview.preview_n, 100)


class ApplyDiffTest(_TempCase):
    def test_apply_and_diff(self):
        recipe = self.builtin("classic_ferguson2019_adapted")
        params = recipes.apply(recipe)
        self.assertEqual(params["n_perm"], 5000)
        self.assertEqual(params["design"]["covariates"], ["volume_ml"])
        self.assertEqual(recipes.diff(recipe, params), [])
        self.assertEqual(recipes.custom_label(recipe, params), recipe.title)
        changed = recipes.set_field(params, "n_perm", 1000)
        changed["rule"] = {"kind": "top_pct", "value": 10}
        deviations = recipes.diff(recipe, changed)
        self.assertEqual([item["field"] for item in deviations], ["rule", "n_perm"])
        self.assertEqual(deviations[1]["description"], "n_perm 1000 instead of 5000")
        self.assertEqual(recipes.custom_label(recipe, changed), "Custom (based on Classic sensitivity and specificity (Ferguson 2019))")

    def test_map_parameters_round_trip(self):
        recipe = self.builtin("vdh2026_standard_settings")
        params = recipes.apply(recipe)
        options = recipes.pipeline_options(params, recipe.steps, {"atlas": "none"})
        self.assertEqual((options["nulls"], options["specificity"], options["scores"], options["atlas"]), ("exact", "auto", "none", "none"))
        self.assertEqual(recipes.parameters_from_map(params, rule=params["rule"], options=options), params)
        self.assertEqual(recipes.steps_from_options(recipe.steps, options), recipe.steps)
        options["perm"] = 2000
        options["specificity"] = "off"
        current = recipes.parameters_from_map(params, options=options)
        self.assertEqual([item["field"] for item in recipes.diff(recipe, current)], ["n_perm"])
        self.assertNotIn("specificity", recipes.steps_from_options(recipe.steps, options))
        # The specificity test, null relocations, preview seed and native mode are parameters too
        options = recipes.pipeline_options(params, recipe.steps, {})
        options.update({"specificity": "legacy-welch", "null_relocations": 3, "preview_seed": 4, "native": "skip"})
        current = recipes.parameters_from_map(params, rule=params["rule"], options=options)
        changes = {item["field"]: item["description"] for item in recipes.diff(recipe, current)}
        self.assertEqual(sorted(changes), ["design.method", "native", "nulls.relocations", "seeds.preview"])
        self.assertEqual(changes["design.method"], "specificity test legacy-welch instead of glm")
        back = recipes.pipeline_options(current, recipe.steps, {})
        self.assertEqual((back["specificity"], back["null_relocations"], back["preview_seed"], back["native"]), ("legacy-welch", 3, 4, "skip"))
        self.assertEqual(recipes.parameters_from_map(params, options=dict(options, specificity="auto"))["design"]["method"], "glm")

    def test_malformed_lab_files_are_reported(self):
        good = self.builtin("preview").data
        folder = self.path("bad")
        os.makedirs(folder)
        bad = {"params_text": dict(good, parameters="x"), "design_text": dict(good, parameters={"design": "x"}), "seeds_text": dict(good, parameters={"seeds": "x"}),
               "validation_list": dict(good, parameters={"validation": [1]}), "preview_text": dict(good, parameters={"preview_n": "ten"}),
               "design_key": dict(good, parameters={"design": {"foo": 1}}), "robustness_text": dict(good, parameters={"robustness": {"overlaps": "x"}})}
        for name, data in bad.items():
            self.assertTrue(recipes.validate_recipe(dict(data, id=name)), name)
            with open(os.path.join(folder, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump(dict(data, id=name), f)
        errors = []
        loaded = recipes.load_recipes(folders=[folder], errors=errors)
        self.assertEqual(sorted(recipe.id for recipe in loaded), sorted(recipe.id for recipe in recipes.load_recipes()))  # the built-ins still load
        self.assertEqual(sorted(os.path.basename(path) for path, _message in errors), sorted(f"{name}.json" for name in bad))

    def test_lab_recipe_is_shareable(self):
        base = self.builtin("classic_ferguson2019_adapted")
        params = recipes.set_field(recipes.apply(base), "n_perm", 10000)
        folder = self.path("lab")
        saved = recipes.save_lab_recipe(os.path.join(folder, "our_lab.json"), params, base, title="Our lab classic")
        with open(saved.path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn(self.tmp, text)  # no local path
        data = json.loads(text)
        self.assertEqual((data["status"], data["based_on"]["id"], data["based_on"]["sha256"]), ("lab", base.id, base.sha256))
        self.assertIn("n_perm 10000 instead of 5000", data["differences_from_source"][0])
        loaded = recipes.load_recipes(folders=[folder])
        lab = recipes.find_recipe("our_lab_classic", loaded)
        self.assertEqual((lab.origin, recipes.apply(lab)["n_perm"]), ("folder", 10000))
        self.assertEqual(lab.status_text(), f"lab recipe (based on {base.title})")
        # A lab recipe cannot take a built-in id; a broken file is reported, not fatal
        recipes.save_lab_recipe(os.path.join(folder, "clash.json"), params, base, title="Clash", recipe_id="preview")
        with open(os.path.join(folder, "broken.json"), "w", encoding="utf-8") as f:
            f.write("{")
        errors = []
        loaded = recipes.load_recipes(folders=[folder], errors=errors)
        self.assertEqual(sum(recipe.id == "preview" for recipe in loaded), 1)
        self.assertEqual(sorted(os.path.basename(path) for path, _message in errors), ["broken.json", "clash.json"])
        with self.assertRaises(recipes.RecipeError):
            recipes.save_lab_recipe(os.path.join(folder, "x.json"), params, base, title="Recommended settings")
        # A study's own recipes folder is read too
        study = self.make_study()
        os.makedirs(os.path.join(study.path, recipes.STUDY_RECIPES_DIR))
        shutil.copy(saved.path, os.path.join(study.path, recipes.STUDY_RECIPES_DIR, "our_lab.json"))
        self.assertEqual(recipes.find_recipe("our_lab_classic", recipes.load_recipes(study=study)).origin, "study")


class PlanLockTest(_TempCase):
    NOW = "2026-10-18T09:30:00+00:00"

    def roi_file(self, study, name="hippocampus"):
        path = os.path.join(study.path, "rois", f"{name}.nii.gz")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = np.zeros((4, 4, 4), dtype=np.uint8)
        data[1:3, 1:3, 1:3] = 1
        nifti.write_nifti(path, data, np.diag([2.0, 2.0, 2.0, 1.0]), dtype=np.uint8)
        return path

    def lock(self, study, now=NOW, **kwargs):
        recipe = self.builtin("classic_ferguson2019_adapted")
        hypotheses = {"primary": {"kind": "contrast", "contrast": "A>B"}, "rois": [self.roi_file(study)], "seeds": {"permutation": 7}}
        return recipes.lock_plan(study, recipe, recipes.apply(recipe), hypotheses, now=now, **kwargs)

    def test_plan_sha_is_stable(self):
        study = self.make_study()
        plan = self.lock(study)
        self.assertEqual(plan["sha256"], recipes.plan_sha256(plan))
        with open(recipes.plan_path(study), encoding="utf-8") as f:
            stored = json.load(f)
        self.assertEqual(stored, plan)
        self.assertEqual(recipes.plan_sha256(stored), plan["sha256"])
        roi_entry = plan["hypotheses"]["rois"][0]
        self.assertEqual((roi_entry["name"], roi_entry["path"], roi_entry["inside_study"]), ("hippocampus", "rois/hippocampus.nii.gz", True))
        self.assertEqual(roi_entry["sha256"], recipes.file_sha256(os.path.join(study.path, "rois", "hippocampus.nii.gz")))
        self.assertNotIn(self.tmp, json.dumps(plan))  # paths relative to the study
        self.assertEqual(plan["parameters"]["seeds"]["permutation"], 7)
        # The same inputs give the same sha256 in another folder
        other = Study.create(self.path("copy", "study"), name="study")
        other.save()
        self.assertEqual(self.lock(other)["sha256"], plan["sha256"])
        # Status chip
        status = recipes.plan_status(study)
        self.assertEqual(status["state"], "locked")
        self.assertEqual(status["chip"], f"Plan: locked 2026-10-18 · {plan['sha256'][:8]}")
        self.assertEqual(recipes.plan_status(self.make_study("open"))["chip"], "Plan: open")

    def test_lock_twice_and_tampering(self):
        study = self.make_study()
        plan = self.lock(study)
        with self.assertRaises(recipes.RecipeError):
            self.lock(study)
        replaced = self.lock(study, now="2026-10-19T10:00:00+00:00", supersede_reason="ROI added after the pilot")
        self.assertEqual(replaced["supersedes"]["sha256"], plan["sha256"])
        self.assertTrue(os.path.isfile(os.path.join(study.path, "derivatives", "lnm", f"analysis_plan.superseded-{plan['sha256'][:8]}.json")))
        self.assertEqual(recipes.read_deviations(study)[0]["field"], "plan")
        # Editing the locked plan by hand is visible
        path = recipes.plan_path(study)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data["parameters"]["n_perm"] = 1000
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        self.assertEqual(recipes.plan_status(study)["state"], "modified")

    def test_hypotheses_validation(self):
        study = self.make_study()
        recipe = self.builtin("siddiqi2021_slnm")
        with self.assertRaises(recipes.RecipeError):
            recipes.lock_plan(study, recipe, recipes.apply(recipe), {})
        with self.assertRaises(recipes.RecipeError):
            recipes.lock_plan(study, recipe, recipes.apply(recipe), {"primary": {"kind": "score", "score": "nihss"}, "rois": ["missing.nii.gz"]})
        plan = recipes.lock_plan(study, recipe, recipes.apply(recipe), {"primary": {"kind": "score", "score": "nihss"}, "n_perm": 10000})
        self.assertEqual(plan["hypotheses"]["primary"], {"kind": "score", "score": "nihss", "direction": "two_sided"})
        self.assertEqual(plan["parameters"]["n_perm"], 10000)
        self.assertEqual([item["field"] for item in plan["deviations_from_recipe"]], ["n_perm"])

    def test_deviation_log_and_methods(self):
        study = self.make_study()
        with self.assertRaises(recipes.RecipeError):
            recipes.record_deviation(study, "n_perm", 5000, 1000, "time")  # no plan yet
        plan = self.lock(study)
        current = recipes.set_field(plan["parameters"], "n_perm", 1000)
        pending = recipes.pending_deviations(study, current)
        self.assertEqual([(item["field"], item["old"], item["new"]) for item in pending], [("n_perm", 5000, 1000)])
        with self.assertRaises(recipes.RecipeError):
            recipes.record_deviation(study, "n_perm", 5000, 1000, "  ")
        record = recipes.record_deviation(study, "n_perm", 5000, 1000, "the run had to finish today", now="2026-10-20T08:00:00+00:00")
        self.assertEqual(record["plan_sha256"], plan["sha256"])
        self.assertEqual(recipes.pending_deviations(study, current), [])
        self.assertEqual(recipes.effective_parameters(study)["n_perm"], 1000)
        self.assertEqual(recipes.plan_status(study)["deviations"], 1)
        # Steps in another order are not a change
        self.assertEqual(recipes.pending_deviations(study, current, steps=list(reversed(plan["steps"]))), [])
        # Steps turned off in Map are deviations too
        self.assertEqual([item["field"] for item in recipes.pending_deviations(study, current, steps=[step for step in plan["steps"] if step != "specificity"])], ["steps"])
        # The methods text (L-18) reports the deviation with its reason
        text = methods.find_section(methods.methods_sections(study.path), "deviations").text()
        self.assertIn("n_perm 1000 instead of 5000 (reason: the run had to finish today", text)
        # The plan's own record is unchanged by deviations
        self.assertEqual(recipes.plan_status(study)["state"], "locked")
        self.assertEqual(recipes.to_pipeline_config(self.builtin("classic_ferguson2019_adapted"), study)["config"]["perm"], 1000)
        self.assertEqual(recipes.to_pipeline_config(self.builtin("classic_ferguson2019_adapted"), study)["recipe"]["plan_sha256"], plan["sha256"])
        # A changed specificity test is a deviation; an invalid value in a hand-edited log is skipped
        legacy = recipes.set_field(current, "design.method", "legacy-welch")
        self.assertEqual([item["description"] for item in recipes.pending_deviations(study, legacy)], ["specificity test legacy-welch instead of glm"])
        recipes.record_deviation(study, "n_perm", 1000, 10, "hand edit")
        self.assertEqual(recipes.effective_parameters(study)["n_perm"], 1000)

    def test_selection(self):
        study = self.make_study()
        self.assertIsNone(recipes.selected_recipe(study))
        recipes.select_recipe(study, self.builtin("preview"))
        self.assertEqual(recipes.selected_recipe(study)["id"], "preview")
        self.lock(study)
        self.assertEqual(recipes.selected_recipe(study)["id"], "classic_ferguson2019_adapted")  # the locked plan wins


class RoiTest(_TempCase):
    def design(self, n_case=6, n_control=6, seed=3):
        rng = np.random.default_rng(seed)
        rows = [(f"sub-{index:02d}", {"group": "case" if index < n_case else "control", "volume_ml": float(rng.uniform(2, 40))}) for index in range(n_case + n_control)]
        study = self.make_study(rows=rows)
        return designlib.build_design(study, designlib.DesignSpec(inference={"n_perm": 1000, "seed": 5})), rng

    def test_roi_means(self):
        maps = np.arange(12, dtype=np.float64).reshape(2, 6)
        masks = np.array([[1, 1, 0, 0, 0, 0], [0, 0, 0, 1, 1, 1]], dtype=bool)
        self.assertTrue(np.allclose(roi.roi_means(maps, masks), [[0.5, 4.0], [6.5, 10.0]]))
        with self.assertRaises(roi.RoiError):
            roi.roi_means(maps, np.zeros(6))

    def test_roi_max_stat_equals_brute_force(self):
        design, rng = self.design()
        maps = rng.standard_normal((design.n, 40))
        maps[: 6, :10] += 0.8  # an effect in the first ROI
        masks = np.zeros((3, 40), dtype=bool)
        masks[0, :10], masks[1, 10:25], masks[2, 25:] = True, True, True
        means = roi.roi_means(maps, masks)
        result = roi.roi_glm(design, means, ["a", "b", "c"], n_perm=500, seed=11)
        # Brute force: refit Y* = P Rz Y + Hz Y for every shuffling; the FWE null is the maximum |t| over ROIs
        shufflings = list(glm.shufflings(design, n_perm=500, seed=11)[0])
        X, C = design.X, design.C
        _, Z = glm.nuisance_partition(X, C)
        Hz = Z @ np.linalg.pinv(Z)
        Yr, Yh = means - Hz @ means, Hz @ means
        stats = []
        for perm in shufflings:
            Ys = Yr[np.asarray(perm)] + Yh
            stats.append(glm.fit(Ys, X, C).stat)
        stats = np.abs(np.array(stats))
        observed = stats[0]
        tol = 1e-9 * observed + 1e-12
        max_null = stats.max(axis=1)
        p_fwe = (np.sum(max_null[1:, None] >= observed[None, :] - tol, axis=0) + 1) / len(shufflings)
        p_unc = (np.sum(stats[1:] >= observed[None, :] - tol, axis=0) + 1) / len(shufflings)
        self.assertTrue(np.allclose(result.stat, glm.fit(means, X, C).stat))
        self.assertTrue(np.array_equal(result.p_fwe, p_fwe), (result.p_fwe, p_fwe))
        self.assertTrue(np.array_equal(result.p_unc, p_unc))
        self.assertTrue(np.all(result.p_fwe >= result.p_unc))
        self.assertLess(result.p_fwe[0], 0.05)
        record = result.to_dict([10, 15, 15])
        self.assertEqual([row["name"] for row in record["rois"]], ["a", "b", "c"])
        self.assertEqual(record["rois"][0]["voxels"], 10)

    def test_small_volume_fwe(self):
        design, rng = self.design(seed=8)
        maps = rng.standard_normal((design.n, 60))
        mask = np.zeros(60, dtype=bool)
        mask[20:35] = True
        result, indices = roi.small_volume_fwe(design, maps, mask, n_perm=400, seed=2)
        self.assertTrue(np.array_equal(indices, np.arange(20, 35)))
        whole = glm.run(design, maps[:, indices], n_perm=400, seed=2, dtype=np.float64)
        self.assertTrue(np.array_equal(result.p_fwe, whole.p_fwe))
        # Restricting the family to the ROI never makes a voxel's FWE p larger than the whole-mask one
        brain = glm.run(design, maps, n_perm=400, seed=2, dtype=np.float64)
        self.assertTrue(np.all(result.p_fwe <= brain.p_fwe[indices] + 1e-12))

    def test_svc_mask_and_cli(self):
        shape = (6, 6, 6)
        affine = np.diag([2.0, 2.0, 2.0, 1.0])
        mask = np.zeros(shape, dtype=np.uint8)
        mask[1:5, 1:5, 1:5] = 1
        roi_volume = np.zeros(shape, dtype=np.uint8)
        roi_volume[0:3, 0:3, 0:3] = 1  # partly outside the mask
        mask_path, roi_path = self.path("mask.nii.gz"), self.path("roiA.nii.gz")
        nifti.write_nifti(mask_path, mask, affine, dtype=np.uint8)
        nifti.write_nifti(roi_path, roi_volume, affine, dtype=np.uint8)
        out = self.path("svc.nii.gz")
        self.assertEqual(roi.write_svc_mask(roi_path, mask_path, out), 8)
        data, out_affine, _header = nifti.read_nifti(out)
        self.assertTrue(np.allclose(out_affine, affine))
        self.assertEqual(int((np.asarray(data) != 0).sum()), 8)
        # ROI GLM command line on synthetic maps
        rng = np.random.default_rng(4)
        rows = [(f"sub-{index:02d}", {"group": "case" if index < 6 else "control", "volume_ml": float(rng.uniform(2, 40))}) for index in range(12)]
        study = self.make_study(rows=rows)
        maps_dir = self.path("maps")
        os.makedirs(maps_dir)
        lines = ["participant_id\tAvgFz\tT"]
        for sid, _values in rows:
            path = os.path.join(maps_dir, f"{sid}.nii.gz")
            nifti.write_nifti(path, rng.standard_normal(shape).astype(np.float32), affine, dtype=np.float32)
            lines.append(f"{sid}\t{os.path.basename(path)}\tn/a")
        with open(os.path.join(maps_dir, "maps.tsv"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        roi_b = self.path("roiB.nii.gz")
        other = np.zeros(shape, dtype=np.uint8)
        other[3:5, 3:5, 3:5] = 1
        nifti.write_nifti(roi_b, other, affine, dtype=np.uint8)
        result_dir = self.path("roi_out")
        proc = subprocess.run([sys.executable, "-m", "LesionNetworkMappingLib.roi", "glm", "--maps", os.path.join(maps_dir, "maps.tsv"), "--participants", os.path.join(study.path, "participants.tsv"),
                               "--mask", mask_path, "--roi", roi_path, "--roi", roi_b, "--perm", "1000", "--seed", "1", "--out", result_dir], cwd=MODULE_DIR, env=_env(), capture_output=True, text=True, timeout=300, check=False)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(os.path.join(result_dir, roi.ROI_GLM_JSON), encoding="utf-8") as f:
            record = json.load(f)
        self.assertEqual([row["name"] for row in record["rois"]], ["roiA", "roiB"])
        self.assertEqual([row["voxels"] for row in record["rois"]], [8, 8])
        self.assertFalse(os.path.exists(os.path.join(result_dir, ".roi-maps.npy")))
        # The same numbers in process
        indices = geometry.mask_indices(mask != 0)
        Y = np.array([np.asarray(nifti.read_nifti(os.path.join(maps_dir, f"{sid}.nii.gz"))[0], dtype=np.float32).reshape(-1)[indices] for sid, _values in rows])
        masks = np.array([roi.read_roi(path, shape, affine, indices) for path in (roi_path, roi_b)])
        design = designlib.build_design(Study.open(study.path), designlib.DesignSpec())
        expected = roi.roi_glm(design, roi.roi_means(Y, masks), n_perm=1000, seed=1)
        self.assertTrue(np.allclose([row["p_fwe"] for row in record["rois"]], expected.p_fwe))


class CliTest(unittest.TestCase):
    def test_list_and_validate(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(recipes.main(["list", "--json"]), 0)
        listed = json.loads(out.getvalue())
        self.assertEqual({item["id"] for item in listed}, set(BUILTIN_IDS))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(recipes.main(["validate", *sorted(glob.glob(os.path.join(recipes.BUILTIN_DIR, "*.json")))]), 0)
        self.assertEqual(out.getvalue().count(": ok"), len(BUILTIN_IDS))


if __name__ == "__main__":
    unittest.main()

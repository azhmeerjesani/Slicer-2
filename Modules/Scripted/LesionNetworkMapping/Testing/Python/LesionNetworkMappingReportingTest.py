"""Unit tests of reporting v2: sectioned methods, the 15-item checklist and battery, the supplementary
case table, the publication manifest and NeuroVault metadata (standard library and numpy only).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingReportingTest.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from LesionNetworkMappingLib import checklist, export, methods, runs, thresholds
from LesionNetworkMappingLib.study import Study

runs.register_kind("cohort")  # registered by cohort.py (numpy) when it is imported

# SPEC_LNM_ADDENDUM §3.11, exact text (both sides of the 2026 debate are cited)
ADDENDUM_DISCLAIMER = (
    "Research use only — not for clinical decisions. Lesion network mapping specificity is debated "
    "(van den Heuvel et al., 2026, Nat Neurosci, doi:10.1038/s41593-025-02196-7; "
    "Siddiqi et al., 2026, bioRxiv, doi:10.64898/2026.02.24.707529; "
    "Zalesky & Cash, 2026, Nat Neurosci, doi:10.1038/s41593-026-02259-3)."
)
VERSIONS = {"python": "3.12.0", "numpy": "2.0.0", "scipy": "1.14.0", "LesionNetworkMappingLib": "1.0"}


def _bundle(gsp=True, **overrides):
    info = {
        "name": "GSP1000 MF",
        "label": "GSP1000MF",
        "manifest_sha256": "a" * 64,
        "space": "MNI152NLin6Asym",
        "n_subjects": 1000,
        "n_voxels": 228483,
        "doi": "10.7910/DVN/ILXIKS",
        "dataset_version": "3.0",
        "licence": export.GSP_LICENCE if gsp else "CC-BY-4.0",
        "acknowledgement": None,
        "gsr": True,
        "preprocessing": {"gsr": True, "pipelineUrl": "https://example.org/pipeline"},
        "mask_sha256": "b" * 64,
        "citation": "Yeo BTT et al. J Neurophysiol. 2011",
    }
    info.update(overrides)
    return info


def _qa(level="amber", percentile=50.0, k=50):
    return {"degree_r": 0.4, "degree_rho": 0.38, "reference": {"mu_r": 0.5, "pc1_r": 0.45, "percentile": percentile, "k": k, "level": level, "partial_pc1_mu": 0.1}}


class _StudyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-reporting-")
        self.root = os.path.join(self.tmp, "study")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_study(self, rows, **settings):
        study = Study.create(self.root, name="Reporting", template={"id": "MNI152NLin6Asym", "res": 1})
        for sid, values in rows:
            study.add_subject(sid, values)
        for key, value in settings.items():
            setattr(study, key, value)
        study.save()
        return study

    def run_dir(self, name):
        path = os.path.join(self.root, "derivatives", "lnm", name)
        os.makedirs(path, exist_ok=True)
        return path

    def register(self, kind, folder, summary=None, files=None, params=None):
        run_id = runs.register(self.root, kind, folder, params=params, software_versions=VERSIONS)
        for name, content in (files or {}).items():
            export.write_json_atomic(os.path.join(folder, name), content)
        runs.update(self.root, run_id, status="done", summary=summary)
        return run_id

    def cohortmaps(self, gsp=True, levels=("amber", "amber"), split_half=False):
        folder = self.run_dir("cohortmaps")
        content = {
            "format": "slicer-lnm-cohortmaps",
            "bundle": _bundle(gsp),
            "n": 1000,
            "subjects": {"n": 1000, "list_name": None, "list_sha256": None},
            "subject_ids_sha256": "c" * 64,
            "statistic": "AvgFz",
            "supersample": 2,
            "probabilistic": True,
            "null": {"k": 50, "seed": 0},
            "qa": {f"sub-0{index + 1}": _qa(level) for index, level in enumerate(levels)},
            "versions": VERSIONS,
        }
        return self.register("cohortmaps", folder, summary={"mapped": len(levels)}, files={"cohortmaps.json": content})

    def glm(self, n_perm=5000, covariates=("volume_ml",), warnings=(), limitations=("source_type is collinear with group",), design_type="cases_vs_control_database"):
        folder = self.run_dir("glm")
        content = {
            "format": "slicer-lnm-glm",
            "status": "done",
            "test": "two_group",
            "design_type": design_type,
            "contrast": {"label": "caseGtControl", "spec": "case > control", "columns": ["case", "control", "volume_ml"]},
            "inputs": {"input": "Fz"},
            "n": 20,
            "n_excluded": 1,
            "covariates": list(covariates),
            "volume_off_reason": None if "volume_ml" in covariates else "volume is the effect of interest",
            "permutation": {"method": "Freedman-Lane (Winkler et al. 2014) with Beckmann partitioning", "scheme": "free", "reason": "groups nested in datasets", "n_perm": n_perm, "exhaustive": False, "seed": 0, "preview": n_perm < 1000},
            "stat": "t",
            "tails": 2,
            "df": 17,
            "min_attainable_p": 1.0 / (n_perm + 1),
            "fwe": {"alpha": 0.05, "n_significant": 0},
            "fdr": {"alpha": 0.05, "floor_voxels": 46},
            "tfce": {"enabled": False},
            "effect_size": {"name": "HedgesG", "formula": "adjusted mean difference / pooled residual SD x (1 - 3/(4 df - 1))"},
            "limitations": list(limitations),
            "warnings": list(warnings),
        }
        return self.register("glm", folder, summary={"n": 20}, files={"glm.json": content}), folder


class MethodsTest(_StudyCase):
    def test_missing_information_is_not_recorded(self):
        self.make_study([("sub-01", {}), ("sub-02", {"include": "0"})])
        sections = methods.methods_sections(self.root)
        ids = [section.id for section in sections]
        self.assertEqual(ids, ["pipeline", "lesions", "seeds", "connectome", "statistics", "thresholds", "qa", "deviations", "limitations", "software", "citations", "disclaimer"])
        text = {section.id: section.text() for section in sections}
        for section_id in ("pipeline", "seeds", "connectome", "thresholds", "qa", "deviations", "limitations"):
            self.assertIn("not recorded", text[section_id], section_id)
        lesions = text["lesions"]
        self.assertIn("1 lesion(s) were included (2 row(s) in participants.tsv; 1 with include = 0)", lesions)
        for phrase in ("Sources: not recorded 1", "PRISMA counts: not recorded", "Dimensionality: not recorded 1", "2D extrusion policy: not recorded", "Raters: not recorded", "Inter-rater agreement: not recorded"):
            self.assertIn(phrase, lesions)
        self.assertIn("not run", text["statistics"])
        self.assertIn("RRID:SCR_005619", text["software"])
        self.assertIn("3D Slicer not recorded", text["software"])
        # Without a study, the lesion intake is "not recorded" too
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(bare)
        self.assertIn("not recorded (no study)", methods.find_section(methods.methods_sections(bare), "lesions").text())

    def test_lesion_intake_counts_and_normalization(self):
        rows = [
            ("sub-01", {"source_type": "case_report", "dimensionality": "2D", "fig_convention": "radiological", "figure": "2", "panel": "B", "trace_method": "figure+smartfill", "assist_pct": "20", "tracer": "AB", "review_status": "approved", "flipped": "1"}),
            ("sub-02", {"source_type": "case_report", "dimensionality": "3D", "trace_method": "manual+smartfill", "assist_pct": "40", "tracer": "CD", "review_status": "pending"}),
            ("sub-03", {"source_type": "own_scan", "dimensionality": "3D", "trace_method": "auto:nnunet", "norm_method": "BRAINSFit standard", "norm_qc": "pass", "lesion_native": "lesions/native/sub-03.nii.gz"}),
        ]
        self.make_study(rows, trace_policy={"extrude_2d": {"mode": "fractional", "total_mm": 2}})
        norm_dir = os.path.join(self.root, "derivatives", "normalization", "sub-03")
        os.makedirs(norm_dir)
        export.write_json_atomic(
            os.path.join(norm_dir, "normalization.json"),
            {
                "format": "slicer-lnm-normalization",
                "status": "done",
                "template": {"id": "MNI152NLin2009cAsym"},
                "method": {"tool": "BRAINSFit", "version": "5.12", "quality": "standard", "grid": [8, 8, 8], "dilate_mm": 3, "two_step": False, "attempts": [{"name": "A1"}, {"name": "A2"}]},
                "qc": {"suggestion": "looks-ok"},
                "review": {"verdict": None},
            },
        )
        sections = methods.methods_sections(self.root)
        lesions = methods.find_section(sections, "lesions").text()
        for phrase in (
            "Sources: published case reports 2, own scans 1",
            "Dimensionality: 2D 1, 3D 2",
            "fractional, exact total thickness of 2 mm (edge slices fractional)",
            "Mirrored left-right: 1 lesion(s)",
            "Raters: 2 (AB, CD); 1 lesion(s) without a recorded rater",
            "states approved 1, pending 1, not recorded 1",
            "QC pass 1 of 1 (100%)",
            "assisted tracing (2 lesions, median 30% of voxels)",
            "Automatic segmentation: 1 lesion(s), 0 approved",
            "BRAINSFit 5.12",
            "lesion dilated by 3 mm",
            "median 2, max 2",
            methods.RESULTS_MAY_DIFFER,
            "Tracing template: MNI152NLin6Asym at 1 mm",
        ):
            self.assertIn(phrase, lesions)
        self.assertIn(methods.METHOD_CITATIONS["brett2001"], methods.find_section(sections, "citations").text())

    def test_statistics_connectome_thresholds_and_render(self):
        self.make_study([("sub-01", {"group": "case"}), ("sub-02", {"group": "control"})])
        self.cohortmaps(gsp=True)
        glm_id, _ = self.glm()
        # An exported run with a threshold rule
        export_dir = self.run_dir("export")
        resolved = thresholds.resolve(thresholds.DEFAULT_RULE, 1000, 228483)
        provenance = {
            "format": export.PROVENANCE_FORMAT,
            "connectome": _bundle(True),
            "N": 1000,
            "subject_ids_sha256": "c" * 64,
            "subject_list": {"name": None, "sha256": None},
            "threshold": {"rule": thresholds.DEFAULT_RULE, "resolved": resolved.to_dict(), "derived": thresholds.describe(resolved)},
            "lesions": [{"name": "sub-01", "qa": _qa()}],
            "software": {"slicer": "5.11.0", "fork_revision": "abc1234"},
            "versions": VERSIONS,
        }
        export.write_json_atomic(os.path.join(export_dir, "provenance.json"), provenance)
        sections = methods.methods_sections(self.root)
        text = {section.id: section.text() for section in sections}
        self.assertIn(glm_id, text["statistics"])
        for phrase in ("cases_vs_control_database", "5000 permutations", "two-sided", "max-statistic FWE", "Benjamini-Hochberg", "HedgesG", "FDR floor", "TFCE was not used", "network associated with"):
            self.assertIn(phrase, text["statistics"])
        self.assertIn("source_type is collinear with group", text["limitations"])
        for phrase in ("doi:10.7910/DVN/ILXIKS", "3.0", "c" * 64, "https://example.org/pipeline", "b" * 64, "GSP-DUT-2014"):
            self.assertIn(phrase, text["connectome"])
        self.assertIn(thresholds.describe(resolved), text["thresholds"])
        self.assertIn("Bonferroni FWE bound", text["thresholds"])
        self.assertIn("lnm-reference-v1", text["qa"])
        self.assertIn("3D Slicer 5.11.0 (RRID:SCR_005619", text["software"])
        self.assertIn("fork revision abc1234", text["software"])
        self.assertIn(methods.METHOD_CITATIONS["winkler2014"], text["citations"])
        # Renderings: the wording lint passes and each format carries the disclaimer
        for fmt in ("md", "html", "txt"):
            rendered = methods.render(sections, fmt, standalone=fmt == "html")
            self.assertEqual(methods.lint_wording(rendered), [], fmt)
            self.assertIn(export.DISCLAIMER if fmt != "html" else "Research use only", rendered)
        html = methods.render(sections, "html")
        self.assertTrue(html.startswith('<section id="methods-pipeline">'))
        self.assertNotIn("<html", html)
        self.assertIn("<!DOCTYPE html>", methods.render(sections, "html", standalone=True))
        table = methods.Section("x", "X").table(["Rule", "Value"], [["fwe", "|T| ≥ 5.22"], ["none", None]])
        self.assertIn("| fwe | \\|T\\| ≥ 5.22 |", methods.render([table], "md"))  # pipes in table cells are escaped
        self.assertIn("| none | not recorded |", methods.render([table], "md"))

    def test_acknowledgement_only_for_gsp_bundles(self):
        self.make_study([("sub-01", {})])
        self.cohortmaps(gsp=False)
        sections = methods.methods_sections(self.root)
        self.assertIsNone(methods.find_section(sections, "acknowledgements"))
        self.assertNotIn(export.GSP_ACKNOWLEDGEMENT, methods.render(sections, "md"))
        shutil.rmtree(self.root)
        self.make_study([("sub-01", {})])
        self.cohortmaps(gsp=True)
        sections = methods.methods_sections(self.root)
        self.assertEqual(methods.find_section(sections, "acknowledgements").blocks, [("p", export.GSP_ACKNOWLEDGEMENT)])
        self.assertEqual([section.id for section in sections][-2:], ["acknowledgements", "disclaimer"])

    def test_pipeline_and_deviations(self):
        self.make_study([("sub-01", {})])
        out = self.run_dir("pipeline")
        export.write_json_atomic(
            os.path.join(out, "pipeline.json"),
            {
                "format": "slicer-lnm-pipeline",
                "stages": [{"name": "maps", "provider": "cohortmaps", "status": "done"}, {"name": "nulls", "provider": "nulls", "status": "skipped", "reason": "estimated 3 h"}],
                "overrides": [{"check": "review_gate", "reason": "pilot"}],
                "exclusions": {"include=0": ["sub-09"]},
            },
        )
        os.makedirs(os.path.join(self.root, "derivatives", "lnm"), exist_ok=True)
        export.write_json_atomic(os.path.join(self.root, "derivatives", "lnm", "analysis_plan.json"), {"locked": True})
        with open(os.path.join(self.root, "derivatives", "lnm", "analysis_plan_deviations.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"description": "n_perm 1000 instead of 5000", "reason": "time"}) + "\n")
        sections = methods.methods_sections(self.root, include_dirs=[out])
        pipeline = methods.find_section(sections, "pipeline").text()
        for phrase in ("maps: provider cohortmaps; done", "nulls: provider nulls; skipped (estimated 3 h)", "Override: check review_gate; reason pilot", "Excluded (include=0): 1 lesion(s)"):
            self.assertIn(phrase, pipeline)
        self.assertIn("n_perm 1000 instead of 5000 (reason: time)", methods.find_section(sections, "deviations").text())
        # A deviation record without a description is "not recorded", never an empty text
        with open(os.path.join(self.root, "derivatives", "lnm", "analysis_plan_deviations.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"reason": "x"}) + "\n")
        deviations = methods.find_section(methods.methods_sections(self.root), "deviations").text()
        self.assertIn("Deviation: not recorded (reason: x)", deviations)
        self.assertNotIn(" (reason: x)", deviations.replace("Deviation: not recorded (reason: x)", ""))

    def test_resolution_not_made_up(self):
        study = Study.create(self.root, name="No template")
        study.add_subject("sub-01", {})
        study.save()
        lesions = methods.find_section(methods.methods_sections(self.root), "lesions").text()
        self.assertIn("Tracing template: not recorded; resolution not recorded", lesions)
        self.assertNotIn("at 1 mm", lesions)

    def test_write_methods_and_wording_lint(self):
        self.make_study([("sub-01", {})])
        out = os.path.join(self.tmp, "out")
        files = export.write_methods(self.root, out)
        for fmt in ("md", "html", "txt"):
            self.assertTrue(os.path.isfile(files[fmt]))
        with open(files["checklist"], encoding="utf-8") as f:
            text = f.read()
        self.assertIn("| A1 |", text)
        self.assertIn("| K15 |", text)
        self.assertEqual(len(files["items"]), 15)
        self.assertEqual(len(files["battery"]), 6)
        self.assertEqual(methods.lint_wording("This network Localizes the symptom; a causal circuit; disease-specific"), ["localizes", "causal circuit", "disease-specific"])
        self.assertEqual(methods.lint_wording("Network localization of symptoms (Boes 2015) is a network associated with the symptom"), [])
        self.assertEqual(methods.lint_wording("Symptoms localized to a network; localizing; localised; Localising"), ["localized", "localizing", "localised", "localising"])


class MethodsTextCompatibilityTest(unittest.TestCase):
    def test_single_run_wrapper(self):
        info = _bundle(True)
        resolved = thresholds.resolve(thresholds.DEFAULT_RULE, 3, 228483, note="preview")
        threshold = {"rule": thresholds.DEFAULT_RULE, "resolved": resolved.to_dict(), "derived": thresholds.describe(resolved)}
        subset = {"preview": True, "partial": False, "n_requested": 3, "random": False, "seed": 0}
        text = export.methods_text(info, 3, subset, threshold, [{"name": "sub-01", "qa": {}}], {"null": {"k": 4, "seed": 1}}, software={"slicer": "5.11"})
        for phrase in ("# Methods", "## Network maps", "## Parameters", "## Lesions and QA", "## Software", "## Citations", "## Acknowledgements", "## Disclaimer", "N = 3 subjects (preview", "relocated to 4 random positions", export.GSP_ACKNOWLEDGEMENT, export.DISCLAIMER):
            self.assertIn(phrase, text)
        self.assertEqual(methods.lint_wording(text), [])


class ChecklistTest(_StudyCase):
    def test_empty_study_is_not_run(self):
        self.make_study([])
        items = checklist.evaluate(self.root)
        self.assertEqual([item["id"] for item in items], ["A1", "A2", "A3", "B4", "C5", "D6", "E7", "F8", "G9", "G10", "H11", "H12", "I13", "J14", "K15"])
        self.assertEqual({item["status"] for item in items}, {checklist.NOT_RUN})
        self.assertEqual([dot["colour"] for dot in checklist.battery(self.root, items=items)], [checklist.GREY] * 6)

    def test_statuses_from_registry(self):
        self.make_study([
            ("sub-01", {"group": "case", "source_type": "case_report", "trace_method": "figure"}),
            ("sub-02", {"group": "control", "source_type": "control_db", "trace_method": "imported"}),
        ])
        maps_id = self.cohortmaps(levels=("amber", "red"))
        glm_id, _ = self.glm(n_perm=500)
        nulls_id = self.register("nulls", self.run_dir("nulls"), summary={"mode": "exact", "k_pool": 20})
        validation_id = self.register("validation", self.run_dir("validation"), summary={"auc": 0.6, "scheme": "lodo"})
        items = {item["id"]: item for item in checklist.evaluate(self.root)}
        expected = {
            "A1": checklist.DONE,
            "A2": checklist.NOT_RUN,
            "A3": checklist.FLAGGED,  # control database with unadjustable confounds
            "B4": checklist.DONE,
            "C5": checklist.DONE,
            "D6": checklist.NOT_RUN,
            "E7": checklist.DONE,
            "F8": checklist.FLAGGED,  # 500 permutations
            "G9": checklist.FLAGGED,  # one map more degree-like than relocated lesions
            "G10": checklist.NOT_RUN,
            "H11": checklist.FLAGGED,  # no baselines recorded
            "H12": checklist.DONE,  # LODO validation
            "I13": checklist.NOT_RUN,
            "J14": checklist.NOT_RUN,
            "K15": checklist.NOT_RUN,
        }
        self.assertEqual({key: item["status"] for key, item in items.items()}, expected)
        self.assertEqual(items["B4"]["evidence"], [maps_id])
        self.assertIn(glm_id, items["F8"]["evidence"])
        self.assertIn("500 permutations", items["F8"]["value"])
        self.assertEqual(items["E7"]["evidence"], [nulls_id])
        self.assertEqual(items["H12"]["evidence"], [validation_id])
        dots = {dot["id"]: dot["colour"] for dot in checklist.battery(self.root, items=list(items.values()))}
        self.assertEqual(dots, {"degree_qa": "amber", "null_reference": "green", "specificity": "amber", "validation": "amber", "robustness": "grey", "provenance": "amber"})

        # The registry, not the participants table, decides: removing the GLM's warnings and adding a
        # baseline-validated run, the sweep, a second connectome and the sharing files turns dots green
        shutil.rmtree(self.root)
        self.make_study([("sub-01", {"group": "case", "source_type": "case_report", "trace_method": "manual"})])
        self.cohortmaps(levels=("amber",))
        second = self.run_dir("cohortmaps2")
        content = {"bundle": _bundle(True, manifest_sha256="d" * 64, name="Other"), "n": 500, "subject_ids_sha256": "e" * 64, "statistic": "AvgFz", "supersample": 2, "probabilistic": True, "qa": {}}
        self.register("cohortmaps", second, files={"cohortmaps.json": content})
        self.glm()
        self.register("nulls", self.run_dir("nulls"), summary={"mode": "exact"})
        self.register("validation", self.run_dir("validation"), summary={"baselines": ["volume", "mu_r"], "scheme": "kfold"})
        self.register("robustness", self.run_dir("robustness"), summary={"split_half": 0.98, "cross_connectome_r": 0.85})
        publication = os.path.join(self.root, "derivatives", "lnm", "publication")
        os.makedirs(publication)
        export.write_json_atomic(os.path.join(publication, "manifest.json"), {"files": [{"path": "x"}], "missing": []})
        items = checklist.evaluate(self.root)
        dots = {dot["id"]: dot["colour"] for dot in checklist.battery(self.root, items=items)}
        self.assertEqual(dots, {"degree_qa": "green", "null_reference": "green", "specificity": "green", "validation": "green", "robustness": "green", "provenance": "green"})
        markdown = checklist.checklist_markdown(items, checklist.battery(self.root, items=items))
        self.assertIn(export.DISCLAIMER, markdown)
        self.assertIn("never block", markdown)

    def test_volume_off_and_overlap_without_null_are_flagged(self):
        self.make_study([("sub-01", {"group": "case"})])
        self.glm(covariates=())
        self.register("cohort", self.run_dir("cohort"), summary={"n_maps": 1})
        items = {item["id"]: item for item in checklist.evaluate(self.root)}
        self.assertEqual(items["F8"]["status"], checklist.FLAGGED)
        self.assertIn("volume covariate off", items["F8"]["value"])
        self.assertEqual(items["E7"]["status"], checklist.FLAGGED)

    def test_dot_colours(self):
        self.assertEqual(checklist.dot_colour([]), checklist.GREY)
        self.assertEqual(checklist.dot_colour([checklist.NOT_RUN, checklist.NA]), checklist.GREY)
        self.assertEqual(checklist.dot_colour([checklist.DONE, checklist.NA]), checklist.GREEN)
        self.assertEqual(checklist.dot_colour([checklist.DONE, checklist.FLAGGED]), checklist.AMBER)
        self.assertEqual(checklist.dot_colour([checklist.DONE, checklist.NOT_RUN]), checklist.AMBER)  # partly run

    def test_wording_item(self):
        self.make_study([("sub-01", {})])
        folder = self.run_dir("report")
        with open(os.path.join(folder, "methods.md"), "w", encoding="utf-8") as f:
            f.write("This network localizes the symptom.\n")
        item = checklist.ITEMS_BY_ID["K15"].evaluate(self.root)
        self.assertEqual(item["status"], checklist.FLAGGED)
        self.assertIn("localizes", item["value"])


    def test_wording_item_under_a_cache_folder(self):
        # A study whose own path contains "cache" is still checked; only cache folders below derivatives/ are skipped
        self.root = os.path.join(self.tmp, "cache", "study")
        self.make_study([("sub-01", {})])
        export.write_methods(self.root, self.run_dir("report"))
        item = checklist.ITEMS_BY_ID["K15"].evaluate(self.root)
        self.assertEqual(item["status"], checklist.DONE, item["value"])
        with open(os.path.join(self.run_dir("cache"), "methods.md"), "w", encoding="utf-8") as f:
            f.write("This network localizes the symptom.\n")
        self.assertEqual(checklist.ITEMS_BY_ID["K15"].evaluate(self.root)["status"], checklist.DONE)


class ExportFilesTest(_StudyCase):
    def test_supplementary_table_order(self):
        study = self.make_study([
            ("sub-01", {"citation": "Smith 2020", "figure": "2", "panel": "A", "age": "61", "sex": "F", "reported_side": "L", "dimensionality": "2D", "volume_ml": "1.5", "trace_method": "figure", "review_status": "approved"}),
            ("sub-02", {"doi": "10.1/xyz"}),
            ("sub-03", {"include": "0"}),
            ("sub-04", {"hemisphere": "R"}),
        ])
        out = os.path.join(self.tmp, "supp.tsv")
        export.supplementary_lesions_tsv(study, out)
        with open(out, encoding="utf-8") as f:
            lines = [line.rstrip("\n").split("\t") for line in f]
        self.assertEqual(lines[0], list(export.SUPPLEMENTARY_COLUMNS))
        self.assertEqual([line[:2] for line in lines[1:]], [["1", "sub-01"], ["2", "sub-02"], ["3", "sub-04"]])  # table order; excluded rows left out
        self.assertEqual(lines[1][2:], ["Smith 2020", "2", "A", "61", "F", "L", "not recorded", "2D", "1.5", "figure", "approved"])
        self.assertEqual(lines[2][2], "doi:10.1/xyz")
        self.assertEqual(lines[2][3:], ["not recorded"] * 10)
        self.assertEqual(lines[3][7], "R")
        # The gallery order of lesionset.json wins (ids it does not list follow in table order)
        os.makedirs(os.path.join(self.root, "derivatives", "lnm"), exist_ok=True)
        export.write_json_atomic(os.path.join(self.root, "derivatives", "lnm", "lesionset.json"), {"gallery": ["sub-04", "sub-03", "sub-01"]})
        self.assertEqual(export.gallery_order(Study.open(self.root)), ["sub-04", "sub-01", "sub-02"])
        export.supplementary_lesions_tsv(self.root, out)
        with open(out, encoding="utf-8") as f:
            self.assertEqual([line.split("\t")[:2] for line in f.read().splitlines()[1:]], [["1", "sub-04"], ["2", "sub-01"], ["3", "sub-02"]])
        # An explicit order skips ids that are not in participants.tsv (a stale gallery entry) and repeats
        export.supplementary_lesions_tsv(self.root, out, order=["sub-02", "sub-99", "sub-02", "sub-01"])
        with open(out, encoding="utf-8") as f:
            self.assertEqual([line.split("\t")[:2] for line in f.read().splitlines()[1:]], [["1", "sub-02"], ["2", "sub-01"]])

    def _touch(self, folder, *names):
        os.makedirs(folder, exist_ok=True)
        for name in names:
            with open(os.path.join(folder, name), "wb") as f:
                f.write(b"x")

    def test_publication_manifest(self):
        self.make_study([("sub-01", {})])
        glm_dir = os.path.join(self.root, "derivatives", "lnm", "glm")
        self._touch(glm_dir, "group_conn-X_desc-caseGtControlTstat_map.nii.gz", "group_conn-X_desc-caseGtControlPFwe_map.nii.gz", "design.json", "design_matrix.tsv", "glm.json")
        self._touch(os.path.join(self.root, "derivatives", "lnm", "maps"), "sub-01_conn-X_desc-T_funcmap.nii.gz", "sub-01_conn-X_desc-RelocatedT_funcmap.nii.gz", "reproduce.sh")
        self._touch(os.path.join(self.root, "derivatives", "lnm", "cache", "k"), "sub-01_conn-X_desc-T_funcmap.nii.gz")
        out = os.path.join(self.root, "derivatives", "lnm", "publication", "manifest.json")
        manifest = export.publication_manifest(self.root, out)
        kinds = {entry["path"]: entry["kind"] for entry in manifest["files"]}
        self.assertEqual(kinds["derivatives/lnm/glm/group_conn-X_desc-caseGtControlTstat_map.nii.gz"], "unthresholded_map")
        self.assertEqual(kinds["derivatives/lnm/glm/group_conn-X_desc-caseGtControlPFwe_map.nii.gz"], "p_map")
        self.assertEqual(kinds["derivatives/lnm/glm/design.json"], "design")
        self.assertEqual(kinds["derivatives/lnm/maps/reproduce.sh"], "reproduce")
        self.assertNotIn("derivatives/lnm/maps/sub-01_conn-X_desc-RelocatedT_funcmap.nii.gz", kinds)  # null volumes are not published
        self.assertFalse(any("/cache/" in path for path in kinds))
        self.assertEqual(manifest["missing"], ["palm_export", "checklist", "neurovault"])
        self.assertTrue(all(not os.path.isabs(path) for path in kinds))
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["missing"], manifest["missing"])
        self.assertEqual(checklist.ITEMS_BY_ID["J14"].evaluate(self.root)["status"], checklist.FLAGGED)

    def test_neurovault_df_per_map_type(self):
        folder = os.path.join(self.tmp, "maps")
        self._touch(folder, "sub-01_conn-X_desc-T_funcmap.nii.gz", "sub-01_conn-X_desc-AvgRFz_funcmap.nii.gz", "sub-01_conn-X_desc-AvgR_funcmap.nii.gz")
        export.write_json_atomic(
            os.path.join(folder, "sub-01_conn-X_lnm.json"),
            {"N": 50, "terms": export.GSP_LICENCE, "connectome": _bundle(True), "maps": {"T": "sub-01_conn-X_desc-T_funcmap.nii.gz", "AvgFz": "sub-01_conn-X_desc-AvgRFz_funcmap.nii.gz", "AvgR": "sub-01_conn-X_desc-AvgR_funcmap.nii.gz"}},
        )
        group = os.path.join(self.tmp, "glm")
        self._touch(group, "group_conn-X_desc-caseGtControlTstat_map.nii.gz", "group_conn-X_desc-caseGtControlHedgesG_map.nii.gz", "group_conn-X_desc-caseGtControlPFwe_map.nii.gz", "circuit_mask.nii.gz")
        export.write_json_atomic(os.path.join(group, "glm.json"), {"n": 20, "df": 17, "contrast": {"label": "caseGtControl", "spec": "case > control", "columns": ["a", "b", "c"]}, "connectome": {"bundle": _bundle(False)}})
        paths = [os.path.join(folder, name) for name in ("sub-01_conn-X_desc-T_funcmap.nii.gz", "sub-01_conn-X_desc-AvgRFz_funcmap.nii.gz")]
        paths += [os.path.join(group, name) for name in ("group_conn-X_desc-caseGtControlTstat_map.nii.gz", "group_conn-X_desc-caseGtControlHedgesG_map.nii.gz", "group_conn-X_desc-caseGtControlPFwe_map.nii.gz", "circuit_mask.nii.gz")]
        out = os.path.join(self.tmp, "share", "neurovault", "images.json")
        content = export.neurovault_metadata(paths, out)
        images = content["images"]
        self.assertEqual([image["map_type"] for image in images], ["T", "Other", "T", "Other", "Other", "R"])
        self.assertEqual([image["statistic_parameters"] for image in images], [49, None, 17, None, None, None])  # N - 1 per lesion; n - p for the GLM
        self.assertEqual([image["number_of_subjects"] for image in images[:3]], [1, 1, 20])
        self.assertEqual({image["modality"] for image in images}, {"fMRI-BOLD"})
        self.assertEqual({image["analysis_level"] for image in images}, {"G"})
        self.assertIn("correlates positively", images[0]["sign_coding"])
        self.assertIn("case > control", images[2]["sign_coding"])
        self.assertIn("Plain p-values", images[4]["sign_coding"])
        self.assertEqual(images[0]["acknowledgement"], export.GSP_ACKNOWLEDGEMENT)  # GSP-tagged map
        self.assertNotIn("acknowledgement", images[2])  # a non-GSP connectome
        self.assertEqual(images[0]["file"], "../maps/sub-01_conn-X_desc-T_funcmap.nii.gz")
        # df = n - p when glm.json has no df
        export.write_json_atomic(os.path.join(group, "glm.json"), {"n": 20, "contrast": {"columns": ["a", "b", "c"]}})
        self.assertEqual(export.neurovault_image(paths[2])["statistic_parameters"], 17)
        self.assertEqual(export.neurovault_image({"path": paths[0], "connectome_n": 1000})["statistic_parameters"], 999)
        with open(out, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["disclaimer"], export.DISCLAIMER)

    def test_neurovault_cohortmaps_cache_entry(self):
        # cohortmaps writes per-lesion maps to a shared cache entry folder: entry.json (bundle name and
        # connectome N), no sidecar and no provenance.json
        self.make_study([("sub-01", {})])
        self.cohortmaps(gsp=True)
        entry = os.path.join(self.root, "derivatives", "lnm", "cache", "conn-GSP1000MF_subj-cccccccc", "k" * 16)
        name = "k_conn-GSP1000MF_desc-T_funcmap.nii.gz"
        self._touch(entry, name)
        export.write_json_atomic(os.path.join(entry, "entry.json"), {"format": "slicer-lnm-cache-entry", "bundle": "GSP1000 MF", "n": 1000})
        path = os.path.join(entry, name)
        bare = export.neurovault_image(path)
        self.assertEqual(bare["statistic_parameters"], 999)  # N - 1 from entry.json
        self.assertEqual(bare["data_use_terms"], "not recorded")  # the licence is unknown without the run
        self.assertNotIn("acknowledgement", bare)
        cohort = os.path.join(self.root, "derivatives", "lnm", "cohortmaps")
        for images in (
            export.neurovault_metadata([path], root=self.root)["images"],  # resolved by bundle name
            export.neurovault_metadata([path], connectome=cohort)["images"],  # the run's folder
            export.neurovault_metadata([path], connectome=os.path.join(cohort, "cohortmaps.json"))["images"],
            [export.neurovault_image({"path": path, "connectome": _bundle(True)})],
        ):
            image = images[0]
            self.assertEqual(image["map_type"], "T")
            self.assertEqual(image["statistic_parameters"], 999)
            self.assertEqual(image["data_use_terms"], export.GSP_LICENCE)
            self.assertEqual(image["acknowledgement"], export.GSP_ACKNOWLEDGEMENT)
            self.assertEqual(image["target_template_image"], "GenericMNI")
            self.assertIn("connectome N = 1000", image["description"])
        # A copied map (no entry.json) takes N and the connectome from the cohortmaps run
        copy = os.path.join(self.tmp, "copied")
        self._touch(copy, name)
        image = export.neurovault_metadata([os.path.join(copy, name)], connectome=cohort)["images"][0]
        self.assertEqual((image["statistic_parameters"], image["data_use_terms"]), (999, export.GSP_LICENCE))
        # A connectome other than the entry's bundle is not applied
        image = export.neurovault_image(path, connectome=_bundle(True, name="Other"))
        self.assertEqual((image["statistic_parameters"], image["data_use_terms"]), (999, "not recorded"))


class DisclaimerTest(unittest.TestCase):
    def test_disclaimer_equals_addendum(self):
        self.assertEqual(export.DISCLAIMER, ADDENDUM_DISCLAIMER)
        self.assertEqual(methods.disclaimer_section().blocks, [("p", ADDENDUM_DISCLAIMER)])
        # Both sides of the debate are cited
        for doi in ("10.1038/s41593-025-02196-7", "10.64898/2026.02.24.707529", "10.1038/s41593-026-02259-3"):
            self.assertIn(doi, export.DISCLAIMER)
        self.assertEqual(methods.lint_wording(export.DISCLAIMER), [])


if __name__ == "__main__":
    unittest.main()

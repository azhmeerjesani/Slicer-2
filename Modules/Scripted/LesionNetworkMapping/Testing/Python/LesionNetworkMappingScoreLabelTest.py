"""Output labels of score columns whose names are not plain ASCII (BB-1 lnm-cohort:non-ascii-score-names-share-one-output).

Output names keep only ASCII letters and digits (BIDS desc labels), so "失語スコア" and "運動スコア" (or "Афазия" and
"Моторика") all became "slnmScore": the second score's sLNM result replaced the first one's (Symptom tab), and Run
everything exported the group maps of the last score only. Checked here:

- the label helpers give different labels to such names, and the labels of ASCII names are unchanged;
- ``slnm --desc`` names the outputs, and Run everything gives every score its own exported group maps;
- the Symptom tab gives a score another folder when the folder of its label holds another score's result (needs the
  Slicer application; skipped under a plain PythonSlicer).

Run without Slicer's GUI::

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingScoreLabelTest.py -v
"""

import json
import os
import re
import shutil
import sys
import tempfile
import types
import unicodedata
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
for folder in (MODULE_DIR, TEST_DIR):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import numpy as np

from LesionNetworkMappingLib import export, glm, pipeline, slnm
from LesionNetworkMappingLib.study import Study

PAIRS = [("失語スコア", "運動スコア"), ("Афазия", "Моторика"), ("NIHSS 失語", "NIHSS 運動")]
LABEL = re.compile(r"[A-Za-z0-9]+")


def _has_slicer():
    try:
        import slicer
    except ImportError:
        return False
    return getattr(slicer, "app", None) is not None


def _continuous(column):
    return types.SimpleNamespace(spec=types.SimpleNamespace(test="continuous", score_column=column, contrast="positive"))


class LabelTest(unittest.TestCase):
    def test_non_ascii_names_get_different_labels(self):
        makers = {
            "slnm.score_label": slnm.score_label,
            "pipeline.score_label": pipeline.score_label,
            "glm.contrast_label": lambda column: glm.contrast_label(_continuous(column)),
            "lodo dataset label": lambda text: glm._upper_first(glm._camel(text)),
        }
        for first, second in PAIRS:
            for name, make in makers.items():
                with self.subTest(maker=name, names=(first, second)):
                    a, b = make(first), make(second)
                    self.assertTrue(LABEL.fullmatch(a) and LABEL.fullmatch(b), (a, b))
                    self.assertNotEqual(a.casefold(), b.casefold())
                    self.assertEqual(make(first), a, "labels are stable")

    def test_ascii_labels_are_unchanged(self):
        self.assertEqual(slnm.score_label("nihss"), "slnmNihss")
        self.assertEqual(slnm.score_label("aphasia_score"), "slnmAphasiaScore")
        self.assertEqual(slnm.score_label("NIHSS"), "slnmNIHSS")
        self.assertEqual(slnm.score_label("___"), "slnmScore")
        self.assertEqual(pipeline.score_label("aphasia_score"), "AphasiaScore")
        self.assertEqual(pipeline.score_label("x_1a b"), "X1aB")
        self.assertEqual(pipeline.score_label(""), "Score")
        self.assertEqual(glm.contrast_label(_continuous("nihss")), "nihssPos")
        self.assertEqual(glm._unicode_tag("aphasia score (0-10)"), "")

    def test_unicode_normal_forms_give_one_label(self):
        composed = "Größe"
        decomposed = unicodedata.normalize("NFD", composed)
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(slnm.score_label(composed), slnm.score_label(decomposed))
        self.assertNotEqual(slnm.score_label(composed), slnm.score_label("Grösse"))

    def test_unique_labels_ignore_case(self):
        labels = pipeline.unique_labels(["NIHSS", "nihss", "aphasia_score_1", "aphasia_score1"])
        self.assertEqual(len({label.casefold() for label in labels}), 4, labels)
        self.assertEqual(labels[0], "NIHSS")
        labels = slnm._unique_labels(["NIHSS", "nihss"], slnm.score_label)
        self.assertNotEqual(labels[0].casefold(), labels[1].casefold())


class PipelineTwoScoresTest(unittest.TestCase):
    """Run everything with two Japanese score columns exports the group maps of both."""

    @classmethod
    def setUpClass(cls):
        import lnm_pipeline_fixture as fixture
        from LesionNetworkMappingPipelineTest import FAST, cli, run_args

        cls.tmp = tempfile.mkdtemp(prefix="lnm-score-label-")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f"), extra_rows=False)
        study = Study.open(cls.fx["study"])
        rng = np.random.default_rng(4)
        cls.columns = list(PAIRS[0])
        for sid in study.subject_ids:
            value = study.value(sid, "aphasia_score", float)
            study.set(sid, {cls.columns[0]: value, cls.columns[1]: round(float(rng.uniform(0, 30)), 2)})
        for column in cls.columns:
            study.set_column_role(column, "score", type="numeric")
        study.save()
        cls.code, cls.events, _stdout, cls.stderr, _wall = cli(*run_args(cls.fx, *FAST, "--specificity", "off", "--no-report", "--scores", ",".join(cls.columns)))
        cls.out = cls.fx["out"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_score_has_its_own_group_maps(self):
        self.assertEqual(self.code, 0, self.stderr[-3000:])
        with open(os.path.join(self.out, pipeline.PIPELINE_JSON), encoding="utf-8") as f:
            results = json.load(f)["cohort"]["slnm"]
        self.assertEqual(sorted(value["column"] for value in results.values()), sorted(self.columns))
        scores = {}
        for name in os.listdir(self.out):
            if re.fullmatch(r"group_conn-.+_desc-slnm[A-Za-z0-9]+R_map\.json", name):
                with open(os.path.join(self.out, name), encoding="utf-8") as f:
                    scores[name] = json.load(f).get("score")
        self.assertEqual(sorted(scores.values()), sorted(self.columns), scores)
        for label, value in results.items():
            stage = os.path.join(self.out, pipeline.STAGES_DIR, f"slnm-{label}")
            names = [name for name in os.listdir(stage) if name.endswith(f"_desc-slnm{label}R_map.nii.gz") and name.startswith("group_conn-")]
            self.assertEqual(len(names), 1, (label, sorted(os.listdir(stage))))
            stage_r, top_r = os.path.join(stage, names[0]), os.path.join(self.out, names[0])
            self.assertTrue(os.path.isfile(top_r), top_r)
            self.assertEqual(export.file_sha256(top_r), export.file_sha256(stage_r), label)


@unittest.skipUnless(_has_slicer(), "needs the Slicer application")
class SymptomFolderTest(unittest.TestCase):
    """The Symptom tab never runs a score into the folder of another score's result."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-symptom-folder-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def output_dir(self, score):
        from LesionNetworkMappingUI.cohort_symptom import SymptomTab

        tab = types.SimpleNamespace(connectomeDir=lambda: self.tmp, score=lambda: score)
        tab.outputName = lambda: slnm.score_label(score)
        return SymptomTab.outputDir(tab)

    def finish(self, folder, score):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, slnm.SLNM_JSON), "w", encoding="utf-8") as f:
            json.dump({"score": score}, f, ensure_ascii=False)

    def test_scores_with_one_label_get_their_own_folders(self):
        for first, second in [*PAIRS, ("aphasia_score_1", "aphasia_score1")]:
            with self.subTest(names=(first, second)):
                folder = self.output_dir(first)
                self.finish(folder, first)
                self.assertEqual(self.output_dir(first), folder, "a rerun of one score keeps its folder")
                other = self.output_dir(second)
                self.assertNotEqual(os.path.normcase(other).casefold(), os.path.normcase(folder).casefold())
                self.finish(other, second)
                self.assertEqual(self.output_dir(second), other)
                self.assertEqual(self.output_dir(first), folder)


if __name__ == "__main__":
    unittest.main()

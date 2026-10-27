"""Tests of the self-contained cohort report (report.py and reportfigs.py, A-04b).

One pipeline run on A-04's synthetic fixture (lnm_pipeline_fixture) produces report.html; the tests check the
file rules (one UTF-8 file, no scripts, data URIs that decode, SVGs that parse, doi.org links only, no local
paths), the sections and their texts, re-rendering (the CLI, no process, byte-identical apart from the
"Generated" line), missing inputs, atlas labels (a synthetic two-label atlas, an incomplete table, a space
mismatch), orientation and colours at the pixel level, the wording lint, progress and cancellation, and the
code rules (reportfigs composes L-23 output only; report.py computes no statistics).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingReportTest.py -v
"""

import base64
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from xml.etree import ElementTree
from unittest import mock

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
for folder in (MODULE_DIR, TEST_DIR):
    if folder not in sys.path:
        sys.path.insert(0, folder)

import numpy as np

import lnm_pipeline_fixture as fixture
from LesionNetworkMappingLib import colormaps, export, figures, jobs, methods, nifti, pipeline, report, reportfigs, thresholds

SECTIONS = ("header", "summary", "preflight", "lesions", "network-FWE05", "nulls", "specificity", "conjunction", "slnm-AphasiaScore", "qa", "clusters", "gallery", "methods", "checklist", "reproduce", "files")
NOW = "2026-01-01T00:00:00Z"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def section_ids(text):
    return re.findall(r'<section id="([^"]+)"', text)


def section_html(text, section_id):
    start = text.index(f'<section id="{section_id}"')
    end = text.find('<section id="', start + 10)
    while end != -1 and text.startswith('<section id="methods-', end):  # L-18's methods sections are nested
        end = text.find('<section id="', end + 10)
    return text[start:end if end != -1 else len(text)]


def without_generated(text):
    return "\n".join(line for line in re.sub(r"Generated [^<]*", "Generated", text).splitlines())


def copy_out(out, name):
    """A copy of the output folder next to it (so that pipeline.json's relative study path stays valid)."""
    target = os.path.join(os.path.dirname(out), name)
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(out, target, ignore=shutil.ignore_patterns("report.html", "*.tmp"))
    return target


def edit_json(path, change):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    change(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def ras_grid(shape=(40, 48, 40), spacing=2.0, origin=(-39.0, -47.0, -39.0)):
    affine = np.diag([spacing, spacing, spacing, 1.0])
    affine[:3, 3] = origin
    return shape, affine


def world_index(affine, xyz):
    return tuple(round(v) for v in (np.linalg.inv(affine) @ np.append(np.asarray(xyz, dtype=np.float64), 1.0))[:3])


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="lnm-report-")
        cls.fx = fixture.make_fixture(os.path.join(cls.tmp, "f1"))
        args = pipeline._parser().parse_args(["run", "--study", cls.fx["study"], "--bundle", cls.fx["bundle"], "--accept-terms", "--perm", "1000", "--events", "json"])
        cls.events = []
        cls.code = pipeline.run(pipeline.config_from_args(args), emit=pipeline.Emitter("json", sink=lambda event, record: cls.events.append(record)))
        cls.out = cls.fx["out"]
        cls.path = os.path.join(cls.out, report.REPORT_NAME)
        cls.text = read(cls.path) if os.path.isfile(cls.path) else ""
        cls.pipeline_json = json.loads(read(os.path.join(cls.out, pipeline.PIPELINE_JSON)))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # The file

    def test_report_file(self):
        self.assertEqual(self.code, 0)
        stage = next(stage for stage in self.pipeline_json["stages"] if stage["name"] == "report")
        self.assertEqual(stage["status"], "done", stage)
        self.assertEqual(self.pipeline_json["outputs"]["report"], "report.html")
        with open(self.path, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-8")
        self.assertNotIn("<script", text.lower())
        self.assertIsNone(re.search(r"""src=["']?https?:""", text, re.IGNORECASE))
        for href in re.findall(r'href="([^"]+)"', text):
            if re.match(r"[a-z]+:", href):
                self.assertTrue(href.startswith("https://doi.org/"), href)
            else:
                self.assertFalse(os.path.isabs(href), href)
        self.assertNotIn(self.tmp, text, "no local path in a shared file")
        self.assertNotIn(os.path.basename(self.tmp), text)
        images = re.findall(r"<img ([^>]*)>", text)
        self.assertGreater(len(images), 12)
        from PIL import Image

        for attributes in images:
            source = re.search(r'src="data:image/png;base64,([^"]+)"', attributes)
            self.assertIsNotNone(source, attributes[:80])
            self.assertRegex(attributes, r'alt="[^"]+"')
            image = Image.open(io.BytesIO(base64.b64decode(source.group(1))))
            image.load()
            self.assertEqual(image.format, "PNG")
        svgs = re.findall(r"<svg.*?</svg>", text, re.DOTALL)
        self.assertGreater(len(svgs), 3)
        for svg in svgs:
            element = ElementTree.fromstring(svg)
            self.assertEqual(element.get("role"), "img")
            self.assertTrue(element.get("aria-label"))
        self.assertIn("<figcaption>", text)
        self.assertIn("<th", text)
        css = re.search(r"<style>(.*?)</style>", text, re.DOTALL).group(1)
        self.assertLessEqual(len(css.encode("utf-8")), 3072)
        styled = set(re.findall(r"\.([a-z][\w-]*)", css))
        for svg in svgs:
            for names in re.findall(r'class="([^"]+)"', svg):
                for name in names.split():
                    if name not in ("cbar", "plot", "pstrip"):
                        self.assertIn(name, styled, f"SVG class {name!r} has no rule in the report CSS")
        for token in ("prefers-color-scheme:dark", "data-theme=dark", "data-theme=light", "@media print", "image-rendering:pixelated", "#E3B341", "#9A6700", "#F85149", "max-width:1100px", "tabular-nums"):
            self.assertIn(token, css)

    def test_sections_and_texts(self):
        text = self.text
        ids = [section_id for section_id in section_ids(text) if not section_id.startswith("methods-")]
        self.assertEqual(ids, [*SECTIONS, "footer"])
        disclaimer = html.escape(export.DISCLAIMER, quote=True)
        self.assertGreaterEqual(text.count(disclaimer), 2)  # top, bottom (and L-18's methods disclaimer)
        self.assertIn(disclaimer, section_html(text, "header"))
        self.assertIn(disclaimer, section_html(text, "footer"))
        for entry in self.pipeline_json["thresholds"]:
            described = thresholds.describe(thresholds.Resolved.from_dict(entry["resolved"]))
            self.assertIn(html.escape(described), section_html(text, "summary"))
        self.assertIn("% of in-mask voxels above the rule", text)
        self.assertNotIn("PREVIEW", text)
        nulls = section_html(text, "nulls")
        self.assertIn(html.escape(methods.NULL_CAPTION), nulls)
        self.assertIn("minimum attainable p", nulls)
        self.assertIn(html.escape(report.MEAN_CAPTION), section_html(text, "network-FWE05"))
        self.assertIn(report.CLUSTER_NOTE, section_html(text, "clusters"))
        self.assertIn("Specificity (label permutation + volume covariate)", section_html(text, "header"))
        self.assertEqual(section_html(text, "header").count("<li class="), 6)
        checklist = section_html(text, "checklist")
        self.assertEqual(len(re.findall(r"<tr><td>[A-K]\d+</td>", checklist)), 15)
        gallery = section_html(text, "gallery")
        self.assertEqual(gallery.count("<img "), 12)
        self.assertIn("<details open>", gallery)
        self.assertIn("#1 sub-01", gallery)
        self.assertIn("not available", section_html(text, "qa"))
        self.assertIn(html.escape(report.NOT_AVAILABLE_QA), section_html(text, "qa"))
        self.assertIn("network associated with", text)
        self.assertEqual(report.lint_wording(text), [])
        self.assertIn("Atlas label", section_html(text, "clusters"))
        self.assertIn("<td>n/a</td>", section_html(text, "clusters"), "without an atlas, labels read n/a")
        self.assertIn("ACCEPT_TERMS", section_html(text, "reproduce"))
        self.assertIn("C:\\path\\to\\PythonSlicer.exe", section_html(text, "reproduce"))
        files = section_html(text, "files")
        self.assertIn('href="pipeline.json"', files)
        self.assertNotIn('href="report.html"', files)
        self.assertIn("Neurological convention", text)

    def test_watermark(self):
        """L-53: a tutorial study (TUTORIAL DATA) or a synthetic connectome (SYNTHETIC) is named in the title and a
        header banner, and every map figure carries the watermark (bug bash BB-1: reports had none).
        """
        from PIL import Image

        def title(text):
            return re.search(r"<title>([^<]*)</title>", text).group(1)

        def overlap_png(text):
            source = re.search(r'<img src="data:image/png;base64,([^"]+)"[^>]*alt="Lesion overlap montage"', text).group(1)
            return np.asarray(Image.open(io.BytesIO(base64.b64decode(source))).convert("RGB")).astype(int)

        # The fixture's connectome is synthetic
        self.assertIn("(SYNTHETIC)", title(self.text))
        self.assertRegex(section_html(self.text, "header"), r'<p class="banner watermark" role="alert">SYNTHETIC: ')
        # A tutorial study wins over the synthetic connectome (figures.watermark_for)
        study_json = os.path.join(self.fx["study"], "study.json")
        original = read(study_json)

        def restore():
            with open(study_json, "w", encoding="utf-8") as f:
                f.write(original)

        self.addCleanup(restore)
        edit_json(study_json, lambda data: data.update(tutorial=True))
        tutorial = read(report.render_report(copy_out(self.out, "copy-tutorial"), now=NOW, strict=True))
        self.assertIn("(TUTORIAL DATA)", title(tutorial))
        self.assertRegex(section_html(tutorial, "header"), r'<p class="banner watermark" role="alert">TUTORIAL DATA: ')
        self.assertEqual(report.lint_wording(tutorial), [])
        # Without a watermark the page and the figures carry none
        with mock.patch.object(report._Report, "_watermark", return_value=None):
            plain = read(report.render_report(copy_out(self.out, "copy-nowatermark"), now=NOW, strict=True))
        self.assertNotIn("TUTORIAL DATA", plain)
        self.assertNotIn("banner watermark", plain)
        marked, unmarked = overlap_png(tutorial), overlap_png(plain)
        self.assertEqual(marked.shape, unmarked.shape)
        changed = np.abs(marked - unmarked).sum(axis=2) > 0
        self.assertGreater(int(changed.sum()), 50, "the watermark is burnt into the report figures")
        height, width = changed.shape
        rows, columns = np.nonzero(changed)
        self.assertLess(abs(rows.mean() - height / 2), height / 4, "centred")
        self.assertLess(abs(columns.mean() - width / 2), width / 4, "centred")
        # Every canvas figure records and draws it
        canvas, shape, affine = self.canvas()
        marked_canvas = reportfigs.Canvas(canvas.template, watermark="tutorial")
        lesion = np.zeros(shape, dtype=np.uint8)
        lesion[18:22, 22:26, 18:22] = 1
        volume = figures.Volume(lesion, affine, "lesion")
        for name, call in (("montage", lambda c: c.montage(volume, "lesion", slices=[0.0])),
                           ("ortho", lambda c: c.ortho(volume, (0.0, 0.0, 0.0))),
                           ("gallery_strip", lambda c: c.gallery_strip(volume, None, None, 1.0, label="1"))):
            with_mark, without = call(marked_canvas), call(canvas)
            self.assertEqual(with_mark.meta["watermark"], "TUTORIAL DATA", name)
            self.assertIsNone(without.meta["watermark"], name)
            self.assertFalse(np.array_equal(np.asarray(with_mark.image), np.asarray(without.image)), name)

    def test_preview_overrides_and_missing_inputs(self):
        out = copy_out(self.out, "copy-preview")

        def change(data):
            data["config"]["preview_n"] = 10
            data["overrides"] = [{"what": "no_volume_covariate", "reason": "volume not recorded for every lesion"}]
            data["cohort"]["specificity"] = None
            data["cohort"]["slnm"] = {}
            for stage in data["stages"]:
                if stage["name"] == "specificity":
                    stage.update(status="skipped", reason="no control group (every mapped lesion is a case)")
                if stage["name"] == "slnm":
                    stage.update(status="skipped", reason="no confirmed score column")

        edit_json(os.path.join(out, pipeline.PIPELINE_JSON), change)
        text = read(report.render_report(out, now=NOW, strict=True))
        self.assertIn("PREVIEW", section_html(text, "header"))
        self.assertRegex(section_html(text, "summary"), r'<span class="amber">no_volume_covariate: volume not recorded for every lesion</span>')
        self.assertIn("not computed: no control group (every mapped lesion is a case)", section_html(text, "specificity"))
        self.assertIn("not computed: no confirmed score column", section_html(text, "slnm"))
        self.assertNotIn('id="slnm-AphasiaScore"', text)

    def test_nulls_off_shows_reason(self):
        """Bug bash BB-1: with null models off the section read 'Null models (); K = n/a ...' instead of the reason."""
        reason = "null models are off (--nulls off)"
        out = copy_out(self.out, "copy-nullsoff")

        def change(data):
            data["cohort"]["nulls"] = {"reason": reason}  # what pipeline._skip records
            for stage in data["stages"]:
                if stage["name"] == "nulls":
                    stage.update(status="skipped", reason=reason)

        edit_json(os.path.join(out, pipeline.PIPELINE_JSON), change)
        nulls = section_html(read(report.render_report(out, now=NOW, strict=True)), "nulls")
        self.assertIn(f"not computed: {html.escape(reason)}", nulls)
        self.assertNotIn("n/a null cohorts", nulls)
        self.assertNotIn("()", nulls)

        # a record without a stage entry still shows its own reason
        def change2(data):
            data["stages"] = [stage for stage in data["stages"] if stage["name"] != "nulls"]

        edit_json(os.path.join(out, pipeline.PIPELINE_JSON), change2)
        nulls = section_html(read(report.render_report(out, now=NOW, strict=True)), "nulls")
        self.assertIn(f"not computed: {html.escape(reason)}", nulls)

    def test_no_template(self):
        out = copy_out(self.out, "copy-notemplate")
        with mock.patch.object(reportfigs, "find_template", return_value=None):
            text = read(report.render_report(out, now=NOW, strict=True))
        self.assertIn("no template found", text)
        from PIL import Image

        source = re.search(r'<img src="data:image/png;base64,([^"]+)"[^>]*alt="Lesion overlap montage"', text).group(1)
        image = np.asarray(Image.open(io.BytesIO(base64.b64decode(source))).convert("RGB"))
        self.assertGreater(float((image.sum(axis=2) == 0).mean()), 0.3, "black background without a template")

    def test_no_template_no_group_maps(self):
        out = copy_out(self.out, "copy-nogroups")

        def change(data):
            data["outputs"]["group"] = []
            for stage in data["stages"]:
                if stage["name"] == "overlap":
                    stage.update(status="failed", reason="overlap failed (test)")

        edit_json(os.path.join(out, pipeline.PIPELINE_JSON), change)
        with mock.patch.object(reportfigs, "find_template", return_value=None):
            text = read(report.render_report(out, now=NOW, strict=True))
        self.assertIn("no template found", text)
        self.assertEqual(section_html(text, "gallery").count("<img "), 12, "per-lesion maps set the display grid")
        self.assertIn("Network maps not computed: overlap failed (test)", section_html(text, "network-FWE05"))
        self.assertIn("not computed (no lesionCount map)", section_html(text, "lesions"))

        def no_images(data):
            data["outputs"]["sidecars"] = []
            for row in data["inputs"]["rows"]:
                row["snapshot"] = None

        edit_json(os.path.join(out, pipeline.PIPELINE_JSON), no_images)
        with mock.patch.object(reportfigs, "find_template", return_value=None):
            text = read(report.render_report(out, now=NOW, strict=True))
        self.assertNotIn("<img ", text)
        self.assertIn("lesion snapshot not found", section_html(text, "gallery"))

    # Re-rendering

    def test_rerender_cli_without_process_and_deterministic(self):
        out = copy_out(self.out, "copy-rerender")
        real = subprocess.Popen
        calls = []

        def counting(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        events = []
        started = time.monotonic()
        with mock.patch("subprocess.Popen", side_effect=counting), mock.patch.object(jobs, "emit", side_effect=lambda event, **fields: events.append((event, fields))):
            code = pipeline.main(["report", "--out", out])
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0)
        self.assertEqual(calls, [], "re-rendering starts no process")
        self.assertLessEqual(elapsed, 5.0)
        self.assertTrue(any(event == "progress" and fields.get("stage") == "report" for event, fields in events))
        first = read(os.path.join(out, report.REPORT_NAME))
        second = read(report.render_report(out))
        self.assertEqual(without_generated(first), without_generated(second))
        changed = [line for line in first.splitlines() if line not in second.splitlines()]
        self.assertTrue(all("Generated" in line for line in changed), changed[:2])
        self.assertEqual(read(report.render_report(out, now=NOW)), read(report.render_report(out, now=NOW)))

    def test_progress_and_cancel(self):
        out = copy_out(self.out, "copy-cancel")
        events = []
        report.render_report(out, emit=lambda event, **fields: events.append((event, fields)), now=NOW)
        progress = [fields for event, fields in events if event == "progress"]
        done = [fields["done"] for fields in progress]
        self.assertEqual(sorted(set(done)), list(range(1, max(done) + 1)))
        self.assertEqual(done, sorted(done))
        self.assertEqual(progress[-1]["done"], progress[-1]["total"], "the last progress event reaches 100%")
        self.assertTrue(all(fields["stage"] == "report" for event, fields in events if event == "progress"))
        os.remove(os.path.join(out, report.REPORT_NAME))
        calls = []

        def cancel():
            calls.append(1)
            return len(calls) > 3

        with self.assertRaises(jobs.Cancelled):
            report.render_report(out, cancel=cancel, now=NOW)
        self.assertFalse(os.path.exists(os.path.join(out, report.REPORT_NAME)), "a cancelled render writes nothing")
        self.assertEqual([name for name in os.listdir(out) if name.endswith(".tmp")], [])

    def test_wrong_format_refused(self):
        out = copy_out(self.out, "copy-format")
        edit_json(os.path.join(out, pipeline.PIPELINE_JSON), lambda data: data.update(format="slicer-lnm-run"))
        with self.assertRaises(report.ReportError):
            report.render_report(out)
        with self.assertRaises(report.ReportError):
            report.render_report(os.path.join(self.tmp, "nowhere"))

    # Atlas labels (L-15)

    def write_atlas(self, name, table):
        folder = os.path.join(self.tmp, "atlas")
        os.makedirs(folder, exist_ok=True)
        shape, affine = ras_grid((50, 60, 50), 2.0, (-49.0, -59.0, -49.0))
        x = affine[0, 3] + affine[0, 0] * np.arange(shape[0])
        data = np.where(x[:, None, None] < 0, 1, 2) * np.ones(shape, dtype=np.int16)
        path = os.path.join(folder, name)
        nifti.write_nifti(path, data.astype(np.int16), affine)
        labels = os.path.join(folder, name.replace(".nii.gz", ".tsv"))
        with open(labels, "w", encoding="utf-8") as f:
            f.write(table)
        return path, labels

    def peak_rows(self, text):
        peaks = text.split("Peaks of", 1)[1].split("</table>", 1)[0]
        return re.findall(r"<tr><td>([^<]*)</td><td>[^<]*</td><td>[^<]*</td><td>[^<]*</td><td>([^<]*)</td>", peaks)

    def test_atlas_labels(self):
        out = copy_out(self.out, "copy-atlas")
        atlas, labels = self.write_atlas("tpl-MNI152NLin2009cAsym_atlas-Halves_dseg.nii.gz", "index\tname\n1\tLeft half\n2\tRight half\n")
        events = []
        text = read(report.render_report(out, emit=lambda event, **fields: events.append((event, fields)), atlas=atlas, atlas_labels=labels, now=NOW, strict=True))
        rows = self.peak_rows(text)
        self.assertGreater(len(rows), 3)
        for x, label in rows:
            self.assertEqual(label, "Left half" if float(x) < 0 else "Right half", (x, label))
        self.assertNotIn("labels are approximate", text, "same family: no note")
        self.assertEqual([fields for event, fields in events if event == "warning"], [])
        # An incomplete, 0-based table: "label <n>" and a warning
        atlas2, labels2 = self.write_atlas("tpl-MNI152NLin2009cAsym_atlas-Incomplete_dseg.nii.gz", "index\tname\n0\tBackground\n1\tLeft half\n")
        events = []
        text = read(report.render_report(out, emit=lambda event, **fields: events.append((event, fields)), atlas=atlas2, atlas_labels=labels2, now=NOW))
        labels_found = {label for _x, label in self.peak_rows(text)}
        self.assertIn("label 2", labels_found)
        self.assertTrue(any(event == "warning" and fields.get("code") == "atlas_labels" and "label <n>" in fields["message"] for event, fields in events), events)
        # An atlas of another space family: labels with a caption note
        atlas3, labels3 = self.write_atlas("tpl-MNI152NLin6Asym_atlas-Halves_dseg.nii.gz", "index\tname\n1\tLeft half\n2\tRight half\n")
        text = read(report.render_report(out, atlas=atlas3, atlas_labels=labels3, now=NOW))
        self.assertIn("labels are approximate", section_html(text, "clusters"))

    # Figures (pixel tests)

    def canvas(self, convention=None):
        shape, affine = ras_grid()
        template = np.zeros(shape, dtype=np.float32)
        template[4:-4, 4:-4, 4:-4] = 100.0
        template[8:-8, 8:-8, 8:-8] = 60.0
        return reportfigs.Canvas(figures.Volume(template, affine, "template"), convention=convention), shape, affine

    def test_orientation(self):
        _canvas, shape, affine = self.canvas()
        las = affine.copy()
        las[0, 0], las[0, 3] = -2.0, -affine[0, 3]  # a lesion stored left-to-right reversed (LAS, like GSP grids)
        lesion = np.zeros(shape, dtype=np.uint8)
        i, j, k = world_index(las, (-30.0, 20.0, 0.0))  # patient left, anterior
        lesion[i - 2:i + 3, j - 2:j + 3, k - 1:k + 2] = 1
        for convention, left_half in (("neurological", True), ("radiological", False)):
            canvas, _shape, _affine = self.canvas(convention)
            result = canvas.gallery_strip(figures.Volume(lesion, las, "lesion"), None, None, 1.0, label="1")
            tile = np.asarray(result.image.convert("RGB"))[:, : canvas.grid.shape[0]]
            red = np.argwhere((tile[..., 0] > 180) & (tile[..., 1] < 140) & (tile[..., 2] < 140))
            self.assertGreater(len(red), 4)
            column, row = red[:, 1].mean(), red[:, 0].mean()
            self.assertEqual(column < tile.shape[1] / 2, left_half, (convention, column))
            self.assertLess(row, tile.shape[0] / 2, "anterior is up")
            left, right = figures.side_labels("axial", convention)
            self.assertEqual((left, right), ("L", "R") if left_half else ("R", "L"))
            corner = tile[-12:, :12]
            self.assertTrue((corner == 255).all(axis=2).any(), "a stamp in the bottom-left corner")
            self.assertAlmostEqual(result.meta["centroid"][0], -30.0, delta=1.5)

    def test_colours(self):
        canvas, shape, affine = self.canvas()
        window, threshold = 10.0, 2.0
        values = np.zeros(shape, dtype=np.float32)
        spots = {"pos": (-20.0, 10.0, 0.0), "neg": (20.0, 10.0, 0.0), "low": (0.0, -20.0, 0.0), "pct": (0.0, 20.0, 0.0)}
        index = {name: world_index(affine, xyz) for name, xyz in spots.items()}
        for name, value in (("pos", window), ("neg", -window), ("low", 0.5 * threshold)):
            i, j, k = index[name]
            values[i - 1:i + 2, j - 1:j + 2, k - 1:k + 2] = value
        result = canvas.montage(figures.Volume(values, affine, "map"), "diverging", threshold=threshold, window=window, slices=[0.0], companion=False)
        image = np.asarray(result.image.convert("RGB"))
        empty = np.asarray(canvas.montage(figures.Volume(np.zeros(shape, np.float32), affine, "empty"), "diverging", threshold=threshold, window=window, slices=[0.0], companion=False).image.convert("RGB"))

        def pixel(img, name):
            gi = canvas.grid.index(0, spots[name][0])
            gj = canvas.grid.index(1, spots[name][1])
            return tuple(int(c) for c in img[canvas.grid.shape[1] - 1 - gj, gi])

        def hex_rgb(color):
            return tuple(round(c * 255) for c in colormaps.hex_to_rgb(color))

        self.assertEqual(pixel(image, "pos"), hex_rgb("#FDDB54"))
        self.assertEqual(pixel(image, "neg"), tuple(round(c * 255) for c in colormaps.lnm_diverging()[0]))
        self.assertEqual(pixel(image, "neg"), hex_rgb(colormaps.NEGATIVE_COLORS[1]), "-W is the bright end of the negative half (as built, W2-06)")
        self.assertEqual(pixel(image, "low"), pixel(empty, "low"), "below the threshold: the pure template grey")
        percent = np.zeros(shape, dtype=np.float32)
        i, j, k = index["pct"]
        percent[i - 1:i + 2, j - 1:j + 2, k - 1:k + 2] = 100.0
        sequential = np.asarray(canvas.montage(figures.Volume(percent, affine, "pct"), "sequential", vmax=100.0, slices=[0.0]).image.convert("RGB"))
        self.assertEqual(pixel(sequential, "pct"), hex_rgb("#FCFDBF"))
        png = reportfigs.encode_png(result.image)
        self.assertEqual(png, reportfigs.encode_png(result.image), "deterministic bytes")
        self.assertLessEqual(len(reportfigs.encode_png(result.image, 128)), len(png) + 64)

    def test_find_template(self):
        explicit = os.path.join(self.tmp, "explicit_T1w.nii.gz")
        shape, affine = ras_grid()
        nifti.write_nifti(explicit, np.ones(shape, np.float32), affine)
        self.assertEqual(reportfigs.find_template(explicit), os.path.normpath(explicit))
        self.assertEqual(reportfigs.find_template(None, {"template": explicit}), os.path.normpath(explicit))
        found = reportfigs.find_template(os.path.join(self.tmp, "missing.nii.gz"))
        self.assertTrue(found is None or found.endswith(reportfigs.TEMPLATE_FILE))
        self.assertEqual(reportfigs.template_space(reportfigs.TEMPLATE_FILE), "MNI152NLin2009cAsym")

    # Text helpers

    def test_lint_and_markdown(self):
        self.assertEqual(report.lint_wording("A network associated with aphasia."), [])
        self.assertTrue(report.lint_wording("This lesion localizes the symptom to a disease-specific network."))
        self.assertEqual(report.lint_wording("data:image/png;base64,localizeAAAA"), [])
        with self.assertRaises(ValueError):
            out = copy_out(self.out, "copy-lint")
            edit_json(os.path.join(out, pipeline.PIPELINE_JSON), lambda data: data["warnings"].append("this map localizes the symptom"))
            report.render_report(out, strict=True)
        converted = report.markdown_to_html("# Title\n\n## Part\n\nSome `code` and <script>x</script>.\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("<h3>Title</h3>", converted)
        self.assertIn("<h4>Part</h4>", converted)
        self.assertIn("<code>code</code>", converted)
        self.assertIn("&lt;script&gt;", converted)
        self.assertIn("<li>one</li>", converted)
        self.assertIn("<th scope=\"col\">a</th>", converted)
        self.assertIn("<td>2</td>", converted)
        svg = reportfigs.dot_plot_svg([1.0, 2.0, float("nan"), 3.0], bands=[(0, 1.5, "band-amber")], labels=["a", "b", "c", "d"], title="t <&>")
        self.assertEqual(svg, reportfigs.dot_plot_svg([1.0, 2.0, float("nan"), 3.0], bands=[(0, 1.5, "band-amber")], labels=["a", "b", "c", "d"], title="t <&>"))
        ElementTree.fromstring(svg)
        ElementTree.fromstring(reportfigs.percentile_strip_svg(42.0, 50))
        ElementTree.fromstring(reportfigs.colorbar_svg("sequential", 1, 5, "count", vmax=5))

    # Code rules

    def test_code_rules(self):
        library = os.path.join(MODULE_DIR, "LesionNetworkMappingLib")
        figs = read(os.path.join(library, "reportfigs.py"))
        for banned in ("vtkImageReslice", "map_coordinates", "zoom", "matplotlib", "import vtk", "import slicer", "import qt", "<script"):
            self.assertNotIn(banned, figs)
        self.assertEqual(re.findall(r"#[0-9A-Fa-f]{6}\b", figs), [], "no colour-table literals: colours come from colormaps/figures")
        code = read(os.path.join(library, "report.py"))
        for banned in ("lstsq", "pinv", "default_rng", "np.random", "scipy.stats", "polyfit", "corrcoef", "permutation_test", "import vtk", "import slicer", "matplotlib"):
            self.assertNotIn(banned, code)
        self.assertNotIn("<script", code.lower())


if __name__ == "__main__":
    unittest.main()

"""Self-contained HTML report of a cohort pipeline run: ``<out>/report.html`` (A-04b).

:func:`render_report` reads what the pipeline (A-04) wrote into its output folder and assembles one UTF-8 HTML file
with no scripts and no external resources: images are ``data:image/png;base64`` URIs, plots are inline SVG, links
to output files are relative and the only external links are https://doi.org citations. It opens offline in any
browser, in Slicer's web widget and in the web build, and it renders when the connectome drive is unmounted (the
connectome itself is not read).

The report assembles and computes no statistics: maps and their thresholds come from the pipeline's outputs and
sidecars, figures from :mod:`reportfigs` (over the L-23 renderer :mod:`figures`), labels from :mod:`atlases`
(L-15), methods and checklist from :mod:`methods`/:mod:`checklist` (L-18), cluster tables from :mod:`clusters`
(L-19) and QA chips from :mod:`qa` (L-10). Significance masks only hide voxels whose recorded p is not below alpha.

Sections, by id and in this order: header, summary, preflight, lesions, network-<tag> (one per rule), nulls,
specificity, conjunction, slnm-<Label> (one per score; "slnm" when none), qa, clusters, gallery, methods,
checklist, reproduce, files. The research-only disclaimer appears at the top and at the bottom.

A tutorial study ("TUTORIAL DATA") or a synthetic connectome ("SYNTHETIC", :func:`figures.watermark_for`) is named
in the page title and in a banner under the header, and every map figure carries the same watermark (L-53).

Re-render without computing anything::

    python -m LesionNetworkMappingLib.pipeline report --out DIR [--atlas A --atlas-labels L --template T --figure-convention C]
"""

import csv
import datetime
import html
import json
import math
import os
import re
import urllib.parse

from . import jobs

FORMAT = "slicer-lnm-pipeline"
REPORT_NAME = "report.html"
SECTION_ORDER = ("header", "summary", "preflight", "lesions", "network", "nulls", "specificity", "conjunction", "slnm", "qa", "clusters", "gallery", "methods", "checklist", "reproduce", "files")
MEAN_CAPTION = "average of the lesion network maps (descriptive)"
CLUSTER_NOTE = "descriptive extents only; no cluster-extent inference"
NO_DISPLAY_GRID = "figure not shown: no template or map to set the display grid"
GENERATED = "Generated "
GALLERY_OPEN_MAX = 60
GALLERY_MAX = 500
COVERAGE_FLAG = 0.10  # weight outside the connectome mask above which a lesion is flagged
FILES_PRIVATE = (".pipeline",)
DOI_BASE = "https://doi.org/"
DEBATE_PARAGRAPH = (
    "These checks describe how much each map resembles what any lesion would produce on this connectome (its mean "
    "connectivity profile μC_r and first principal component), calibrated against relocated lesions. A degree-like map "
    "can still be a network associated with the symptom: van den Heuvel et al. (2026) argue that lesion network maps "
    "largely reflect the connectome's degree, Siddiqi et al. (2026) reply that the method's foundations remain sound, "
    "and Zalesky & Cash (2026) propose null models to benchmark it. Read the chips as context, not as a verdict."
)
REFERENCES = (
    ("van den Heuvel et al., 2026, Nat Neurosci", "10.1038/s41593-025-02196-7"),
    ("Siddiqi et al., 2026, bioRxiv", "10.64898/2026.02.24.707529"),
    ("Zalesky & Cash, 2026, Nat Neurosci", "10.1038/s41593-026-02259-3"),
)
WATERMARK_NOTES = {
    "TUTORIAL DATA": "this study was built from a published tutorial dataset to practise the workflow; its maps are a teaching example, not a new analysis.",
    "SYNTHETIC": "these maps were computed on a synthetic connectome (simulated data for testing); they do not describe real brain connectivity.",
}
NOT_AVAILABLE_QA = "not available: no reference maps (μC/PC1) for this connectome and subject list; build them with --build-references"

CSS = (
    ":root{--bg:#F4F4F6;--card:#FFF;--fg:#1C1C1F;--t2:#5C5C66;--line:#E1E1E6;--acc:#2158BD;--ok:#1A7F37;--warn:#9A6700;--bad:#CF222E;--grey:#8E8E98;color-scheme:light}"
    "@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#141416;--card:#232327;--fg:#E8E8EC;--t2:#A1A1AA;--line:#2E2E34;--acc:#6EA3FF;"
    "--ok:#3FB950;--warn:#E3B341;--bad:#F85149;--grey:#71717A;color-scheme:dark}}"
    ":root[data-theme=dark]{--bg:#141416;--card:#232327;--fg:#E8E8EC;--t2:#A1A1AA;--line:#2E2E34;--acc:#6EA3FF;--ok:#3FB950;--warn:#E3B341;--bad:#F85149;--grey:#71717A;color-scheme:dark}"
    "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
    "font-variant-numeric:tabular-nums;overflow-wrap:break-word}main{max-width:1100px;margin:0 auto;padding:12px}"
    "section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 16px;margin:10px 0}"
    "h1{font-size:21px;margin:2px 0}h2{font-size:17px;margin:2px 0 6px}h3{font-size:14.5px;margin:12px 0 4px}a{color:var(--acc)}"
    "img{display:block;max-width:100%;height:auto;image-rendering:pixelated}figure{margin:8px 0;break-inside:avoid}"
    "figcaption,.mut{color:var(--t2);font-size:12.5px}.tw{overflow-x:auto;max-width:100%}table{border-collapse:collapse;font-size:12.5px;margin:4px 0}"
    "th,td{border-bottom:1px solid var(--line);padding:2px 8px;text-align:left;vertical-align:top}th{font-weight:600}"
    ".disc{border:2px solid var(--warn);border-radius:6px;padding:8px 12px;margin:8px 0}"
    ".banner{border:2px solid var(--bad);color:var(--bad);border-radius:6px;padding:6px 12px;font-weight:600}"
    ".amber{color:var(--warn)}.red{color:var(--bad)}.green{color:var(--ok)}.grey{color:var(--grey)}"
    ".chip{display:inline-block;border:1px solid currentColor;border-radius:9px;padding:0 6px;font-size:12px}"
    ".dots{display:flex;flex-wrap:wrap;gap:4px 16px;list-style:none;padding:0;margin:6px 0}.dots li::before{content:'\\25CF';margin-right:4px;font-size:15px}"
    "pre,code{font:12px ui-monospace,Menlo,Consolas,monospace}pre{white-space:pre-wrap;background:var(--bg);padding:8px;border-radius:6px}"
    ".gal{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}.gal figure{margin:0}"
    "svg{max-width:100%;height:auto}svg text,.tick{fill:var(--t2);font-size:10px}.axis{stroke:var(--t2)}.dot{fill:var(--acc);fill-opacity:.8}"
    ".band-amber{fill:var(--warn);opacity:.3}.band-red{fill:var(--bad);opacity:.3}.band-low{fill:var(--ok);opacity:.25}.mark{stroke:var(--fg);stroke-width:2}"
    "section section{border:0;padding:0;margin:6px 0}#methods table{display:block;overflow-x:auto;max-width:100%}.hidden-band{fill:var(--card);opacity:.75}.cb{display:flex;flex-wrap:wrap;align-items:center;gap:0 8px}details>summary{cursor:pointer;font-weight:600;margin:6px 0}"
    "@media print{:root{--bg:#FFF;--card:#FFF;--fg:#1C1C1F;--t2:#5C5C66;--line:#E1E1E6;--acc:#2158BD;color-scheme:light}section,figure,tr{break-inside:avoid}}@media(max-width:600px){main{padding:6px}section{padding:8px}}"
)


class ReportError(ValueError):
    """The output folder cannot be reported (no pipeline.json, or another format)."""


# Small helpers


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_tsv(path):
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f, delimiter="\t"))
    except OSError:
        return None


def _e(value):
    return html.escape("" if value is None else str(value), quote=True)


def _num(value, digits=3):
    if value is None:
        return "n/a"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(value):
        return "n/a"
    if value.is_integer() and abs(value) < 1e9:
        return f"{int(value):,}"
    return f"{value:.{digits}g}"


def _pct(fraction):
    return "n/a" if fraction is None or not math.isfinite(fraction) else f"{100.0 * fraction:.1f}%"


def _duration(seconds):
    if seconds is None:
        return "n/a"
    seconds = float(seconds)
    return f"{seconds:.1f} s" if seconds < 120 else f"{seconds / 60:.1f} min"


def _table(header, rows):
    head = "".join(f'<th scope="col">{_e(cell)}</th>' for cell in header)
    body = "".join("<tr>" + "".join(cell if isinstance(cell, _Raw) else f"<td>{_e(cell)}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


class _Raw(str):
    """A table cell that is already HTML (a <td> element)."""


def _td(content_html, css=""):
    attribute = f' class="{css}"' if css else ""
    return _Raw(f"<td{attribute}>{content_html}</td>")


def _list(items):
    items = [item for item in items if item]
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>" if items else ""


def lint_wording(text):
    """Banned claim phrases in ``text`` (the shared list of :mod:`methods`, L-18); data URIs are ignored."""
    from . import methods

    return methods.lint_wording(re.sub(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+", "", text or ""))


def markdown_to_html(text):
    """Minimal Markdown: #/## headings (as h3/h4), paragraphs, '- ' lists, pipe tables and `code`; everything else escaped."""

    def inline(value):
        parts = re.split(r"(`[^`]+`)", value)
        return "".join(f"<code>{_e(part[1:-1])}</code>" if part.startswith("`") and part.endswith("`") and len(part) > 1 else _e(part) for part in parts)

    out, paragraph, items, table = [], [], [], []

    def flush():
        if paragraph:
            out.append("<p>" + inline(" ".join(paragraph)) + "</p>")
            paragraph.clear()
        if items:
            out.append("<ul>" + "".join(f"<li>{inline(item)}</li>" for item in items) + "</ul>")
            items.clear()
        if table:
            cells = [[cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row.strip().strip("|"))] for row in table if not re.fullmatch(r"\|?[\s:|-]+\|?", row.strip())]
            if cells:
                out.append(_table(cells[0], cells[1:]))
            table.clear()

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("#"):
            flush()
            level = len(stripped) - len(stripped.lstrip("#"))
            out.append(f"<h{min(level + 2, 6)}>{inline(stripped.lstrip('#').strip())}</h{min(level + 2, 6)}>")
        elif stripped.startswith("|"):
            if paragraph or items:
                flush()
            table.append(stripped)
        elif stripped.startswith("- "):
            if paragraph or table:
                flush()
            items.append(stripped[2:])
        else:
            if items or table:
                flush()
            paragraph.append(stripped)
    flush()
    return "\n".join(out)


# The report


class _Report:
    def __init__(self, out_dir, emit, cancel, atlas, atlas_labels, template, convention, now):
        self.out = os.path.abspath(os.fspath(out_dir))
        self.emit = emit
        self.cancel = cancel
        self.now = now
        self.warnings = []
        self.notes = []
        self.done = 0
        self.total = 1
        self.gradient = 0
        data = _read_json(os.path.join(self.out, "pipeline.json"))
        if not isinstance(data, dict):
            raise ReportError(f"No pipeline.json in {os.path.basename(self.out)}: run the pipeline first")
        if data.get("format") != FORMAT:
            raise ReportError(f"pipeline.json has format {data.get('format')!r}, expected {FORMAT!r}")
        self.data = data
        self.config = data.get("config") or {}
        self.cohort = data.get("cohort") or {}
        self.outputs = data.get("outputs") or {}
        self.rows = (data.get("inputs") or {}).get("rows") or []
        self.mapped = [row for row in self.rows if row.get("status") in ("computed", "reused")]
        self.study_path = self._resolve(data.get("study"))
        self.provenance = _read_json(self.path("provenance.json")) or {}
        self.connectome = dict(self.provenance.get("connectome") or {})
        self.connectome.update({key: value for key, value in (data.get("connectome") or {}).items() if key not in self.connectome})
        self.lesionset = (_read_json(self.path("lesionset.json")) or {}).get("summary") or self.cohort.get("lesionset") or {}
        self.qa = _read_json(self.path("qa.json")) or {}
        self.sidecars = self._sidecars()
        self.groups = {item["desc"]: item for item in self.outputs.get("group") or [] if isinstance(item, dict) and item.get("desc")}
        self.alpha = float(self.config.get("alpha") or 0.05)
        self.space = self.connectome.get("space") or self.lesionset.get("space")
        self.convention = convention or self.config.get("figure_convention") or None
        self.watermark = self._watermark()
        self.template = self._template(template)
        self.atlas_arg = atlas or self._path_or_id(self.config.get("atlas"))
        self.atlas_labels_arg = atlas_labels or self._resolve(self.config.get("atlas_labels"))
        self._atlas = None
        self._canvas = None
        self._window = None

    # Paths

    def path(self, *parts):
        return os.path.join(self.out, *parts)

    def _resolve(self, value):
        if not value or not isinstance(value, str):
            return None
        return value if os.path.isabs(value) else os.path.normpath(os.path.join(self.out, value))

    def _path_or_id(self, value):
        """A configuration value that is a path (relative to the output folder) or a catalog id."""
        path = self._resolve(value)
        return path if path and os.path.exists(path) else value or None

    def rel_link(self, rel, text=None):
        return f'<a href="{_e(urllib.parse.quote(rel.replace(os.sep, "/")))}">{_e(text or rel)}</a>'

    def group_path(self, desc):
        item = self.groups.get(desc)
        if item and os.path.isfile(self.path(item["path"])):
            return self.path(item["path"])
        return None

    def group_sidecar(self, desc):
        item = self.groups.get(desc)
        return (_read_json(self.path(item["sidecar"])) or {}) if item and item.get("sidecar") else {}

    def _sidecars(self):
        out = {}
        for name in self.outputs.get("sidecars") or []:
            content = _read_json(self.path(name))
            if isinstance(content, dict):
                sid = (content.get("lesion") or {}).get("name") or name.split("_conn-")[0]
                out[sid] = content
        return out

    def _watermark(self):
        """"TUTORIAL DATA" for a tutorial study, "SYNTHETIC" for a synthetic connectome, else None (L-53)."""
        from . import figures

        study = os.path.join(self.study_path, "study.json") if self.study_path else None
        return figures.watermark_for(study if study and os.path.isfile(study) else None, self.connectome)

    def _template(self, explicit):
        from . import reportfigs

        explicit = explicit or self._resolve(self.config.get("template"))
        return reportfigs.find_template(explicit, self.connectome)

    # Events

    def check_cancel(self):
        cancel = self.cancel
        if cancel is None:
            return
        if hasattr(cancel, "raise_if_cancelled"):
            cancel.raise_if_cancelled(done=self.done)
        elif (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
            raise jobs.Cancelled(done=self.done, reason=getattr(cancel, "reason", None))

    def progress(self):
        self.done += 1
        self.total = max(self.total, self.done)
        if self.emit is not None:
            self.emit("progress", stage="report", done=self.done, total=self.total, unit="figures")
        self.check_cancel()

    def warn(self, message, code="report"):
        self.warnings.append(message)
        if self.emit is not None:
            self.emit("warning", stage="report", code=code, message=message)

    # Figures

    @property
    def canvas(self):
        if self._canvas is None:
            from . import reportfigs

            reference = None if self.template else self.reference_image()
            if self.template is None and reference is None:
                return None
            self._canvas = reportfigs.Canvas(self.template, reference=reference, convention=self.convention, watermark=self.watermark)
        return self._canvas

    def reference_image(self):
        """The image that sets the display grid when no template is found: a group map, else a per-lesion T map,
        else a lesion snapshot; None when the output folder holds no image at all.
        """
        candidates = [self.group_path("lesionCount"), self.group_path("meanT")]
        candidates += [self.path(item["path"]) for item in self.groups.values() if item.get("path")]
        candidates += [self.path((sidecar.get("maps") or {}).get("T")) for sidecar in self.sidecars.values() if (sidecar.get("maps") or {}).get("T")]
        candidates += [self._resolve(row.get("snapshot")) for row in self.mapped if row.get("snapshot")]
        return next((path for path in candidates if path and os.path.isfile(path)), None)

    def figure(self, result, caption, colors=None, extra_html=""):
        from . import reportfigs

        png = result.png(colors or reportfigs.MONTAGE_COLORS)
        scale = result.meta.get("display_scale", 1)
        width, height = result.image.size[0] * scale, result.image.size[1] * scale
        self.progress()
        return (f'<figure><img src="{reportfigs.data_uri(png)}" width="{width}" height="{height}" alt="{_e(result.alt)}">'
                f"<figcaption>{extra_html}{caption}</figcaption></figure>")

    def colorbar(self, kind, low, high, label, threshold=None, vmax=None):
        from . import reportfigs

        self.gradient += 1
        svg = reportfigs.colorbar_svg(kind, low, high, label, threshold=threshold, vmax=vmax)
        svg = svg.replace('id="g"', f'id="g{self.gradient}"').replace("url(#g)", f"url(#g{self.gradient})")
        return f'<div class="cb">{svg} <span class="mut">{_e(label)}</span></div>'

    def orientation_text(self):
        from . import figures

        convention = self.convention or figures.DEFAULT_CONVENTION
        side = "left" if convention == "neurological" else "right"
        text = f"{convention.capitalize()} convention: the left hemisphere is on the image {side} (L/R stamps on every panel; Slicer's own views are radiological)."
        template = os.path.basename(self.template) if self.template else None
        if template is None:
            return text + " Background: no template found (black)."
        from . import reportfigs
        from .geometry import space_family

        space = reportfigs.template_space(template) or template
        text += f" template: {space} (display only)"
        if self.space and space_family(self.space) not in ("unknown", space_family(space)):
            text += f"; the maps are in {self.space}"
        return text + "."

    def montage_html(self, path, kind, caption, threshold=None, sign="both", window=None, vmax=None, color=None, overlays=(), alt="", bar=None):
        if self.canvas is None:
            return f'<p class="mut">{_e(alt)}: {NO_DISPLAY_GRID}</p>'
        result = self.canvas.montage(path, kind, threshold=threshold, sign=sign, window=window, vmax=vmax, color=color, overlays=overlays, alt=alt)
        if not result.meta["slices"]:
            return f'<p class="mut">{_e(alt)}: no slice of the map lies in the display field of view.</p>'
        meta = result.meta
        if bar is None:
            if kind == "diverging":
                bar = self.colorbar("diverging", -meta["window"], meta["window"], "value (shared range ±" + _num(meta["window"]) + ")", threshold=threshold)
            elif kind == "sequential":
                bar = self.colorbar("sequential", 0, vmax or 1, alt, vmax=vmax)
            else:
                bar = ""
        slices = ", ".join(f"{z:+g}" if z else "0" for z in meta["slices"])
        text = f"{caption} Axial slices at z = {slices} mm. {self.orientation_text()}"
        if meta.get("companion"):
            text += " The last row shows the same map unthresholded (companion)."
        return self.figure(result, _e(text), extra_html=bar)

    # Atlas

    def atlas(self):
        if self._atlas is not None or not self.atlas_arg:
            return self._atlas or None
        from . import atlases, nifti, reportfigs
        from .geometry import space_family

        try:
            if os.path.isfile(self.atlas_arg):
                import numpy as np

                data, affine, _header = nifti.read_nifti(self.atlas_arg)
                labels = atlases.read_label_table(self.atlas_labels_arg) if self.atlas_labels_arg else {}
                space = reportfigs.template_space(self.atlas_arg)
                family = space_family(space) if space else atlases.mask_family(np.shape(data)[:3], affine)[0]
                name = os.path.basename(self.atlas_arg).split(".")[0]
                networks = "schaefer" if "schaefer" in name.lower() else None
                atlas = atlases.Atlas(name, data, affine, labels, family, space, networks=networks)
            else:
                atlas = atlases.Atlas.load(self.atlas_arg)  # the cache only: never downloads
        except Exception as exc:  # labels are optional
            self.warn(f"Atlas labels not available ({type(exc).__name__}: {exc}); labels read n/a.", code="atlas")
            self._atlas = False
            return None
        present = set(atlas.region_counts())
        missing = sorted(present - set(atlas.labels))
        if missing:
            self.warn(f"The label table of {atlas.id} has no name for {len(missing)} label value(s) (e.g. {missing[0]}); they read 'label <n>'.", code="atlas_labels")
        self._atlas = atlas
        return atlas

    def label_points(self, points):
        """[(label, network)] of world points; ('n/a', '') without an atlas."""
        atlas = self.atlas()
        if atlas is None or not points:
            return [("n/a", "") for _ in points]
        from . import atlases

        results = atlases.label_points(points, [atlas], space=self.space, allow_cross_family=True)
        out = []
        for result in results:
            if result.allowed_cross_family:
                note = (f"Atlas {atlas.id} ({atlas.family} family) labels maps in {self.space or 'an unrecorded space'}: "
                        "labels are approximate, because templates of different families differ by a few millimetres.")
                if note not in self.notes:
                    self.notes.append(note)
            row = next(iter(result), None)
            label = row[1] if row else None
            out.append((label or atlases.UNLABELLED, (row[2] if row else None) or ""))
        return out

    # Sections

    def build(self):
        from . import export

        self.total = 8 + len(self.data.get("thresholds") or []) * 4 + len(self.cohort.get("slnm") or {}) + min(len(self.mapped), GALLERY_MAX)
        body = [self.header(), self.summary(), self.preflight(), self.lesions()]
        body += self.networks()
        body += [self.nulls(), self.specificity(), self.conjunction()]
        body += self.slnm()
        body += [self.qa_section(), self.clusters(), self.gallery(), self.methods(), self.checklist(), self.reproduce(), self.files()]
        if self.emit is not None and self.total != self.done:  # the start estimate was high: end the bar at 100%
            self.total = self.done
            self.emit("progress", stage="report", done=self.done, total=self.total, unit="figures")
        footer = f'<section id="footer"><div class="disc" role="note">{_e(export.DISCLAIMER)}</div></section>'
        title = f"Lesion network mapping report: {self.study_name()}" + (f" ({self.watermark})" if self.watermark else "")
        page = (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1">\n'
            f"<title>{_e(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<main>\n" + "\n".join(part for part in body if part) + "\n" + footer + "\n</main>\n</body>\n</html>\n"
        )
        return page

    def study_name(self):
        study = _read_json(os.path.join(self.study_path, "study.json")) if self.study_path else None
        name = (study or {}).get("name") if isinstance(study, dict) else None
        return name or (_read_json(self.path("lesionset.json")) or {}).get("study") or (os.path.basename(self.study_path) if self.study_path else "study")

    def section(self, section_id, title, content, details=None):
        if details is not None:
            return f'<section id="{_e(section_id)}"><details{" open" if details else ""}><summary><h2 style="display:inline">{_e(title)}</h2></summary>{content}</details></section>'
        return f'<section id="{_e(section_id)}"><h2>{_e(title)}</h2>{content}</section>'

    # 1. header

    def header(self):
        from . import export

        data, connectome = self.data, self.connectome
        parts = [f"<h1>{_e(self.study_name())}</h1>", '<p class="mut">Lesion network mapping cohort report</p>']
        if self.watermark:
            note = WATERMARK_NOTES.get(self.watermark, "the figures of this report are watermarked.")
            parts.append(f'<p class="banner watermark" role="alert">{_e(self.watermark)}: {_e(note)}</p>')
        identity = [f"Connectome <b>{_e(connectome.get('name') or 'not recorded')}</b>",
                    f"N = {_num(connectome.get('n_subjects') or connectome.get('N'))}",
                    f"V = {_num(connectome.get('n_voxels'))} voxels",
                    f"space {_e(connectome.get('space') or self.space or 'not recorded')}",
                    f"manifest {_e((connectome.get('manifest_sha256') or 'not recorded')[:12])}"]
        for key, label in (("doi", "DOI"), ("dataset_version", "version"), ("gsr", "GSR")):
            if connectome.get(key) is not None:
                value = connectome[key]
                if key == "doi":
                    identity.append(f'DOI <a href="{_e(DOI_BASE + str(value))}">{_e(value)}</a>' if re.fullmatch(r"10\.\S+", str(value)) else f"DOI {_e(value)}")
                else:
                    identity.append(f"{label} {_e(value)}")
        parts.append("<p>" + " · ".join(identity) + "</p>")
        preview = self.config.get("preview_n")
        if preview:
            parts.append(f'<p class="banner" role="alert">PREVIEW: maps of a random subset of {_num(preview)} connectome subjects (seed {_e(self.config.get("preview_seed"))}); not for publication.</p>')
        versions = data.get("versions") or {}
        software = data.get("software") or {}
        status = "finishing (this report was rendered during the run)" if data.get("status") == "running" else data.get("status")
        parts.append(f'<p class="mut">Run {_e(data.get("run_id"))}, created {_e(data.get("created"))}, status {_e(status)}. Software: '
                     + _e(", ".join(f"{key} {value}" for key, value in {**software, **versions}.items() if value)) + "</p>")
        parts.append(f'<p class="mut gen">{GENERATED}{_e(self.now)}</p>')
        parts.append(self.battery())
        parts.append(f'<div class="disc" role="note">{_e(export.DISCLAIMER)}</div>')
        return f'<section id="header">{"".join(parts)}</section>'

    def battery(self):
        dots = self.data.get("battery") or ((self.export_state().get("methods") or {}).get("battery")) or []
        if not dots:
            return '<p class="mut">Battery: not recorded (the methods step did not run).</p>'
        items = []
        for dot in dots:
            colour = dot.get("colour") if dot.get("colour") in ("grey", "green", "amber") else "grey"
            state = {"grey": "not run", "green": "run", "amber": "run and flagged"}[colour]
            items.append(f'<li class="{colour}" title="{_e(dot.get("text"))}"><span style="color:var(--fg)">{_e(dot.get("label"))}</span> <span class="mut">({state})</span></li>')
        return '<h3>Battery</h3><ul class="dots" aria-label="six-check battery">' + "".join(items) + "</ul>"

    def export_state(self):
        return ((_read_json(self.path(".pipeline", "stages", "export.json")) or {}).get("result")) or {}

    # 2. summary

    def summary(self):
        rows, cohort = self.rows, self.cohort
        counts = {"computed": 0, "reused": 0, "excluded": 0}
        for row in rows:
            counts[row.get("status")] = counts.get(row.get("status"), 0) + 1
        reasons = {}
        for row in rows:
            if row.get("status") == "excluded":
                reasons.setdefault(row.get("reason") or "not recorded", []).append(row["participant_id"])
        parts = [_table(["Lesions", "Count"], [["mapped now (computed)", counts["computed"]], ["reused", counts["reused"]], ["excluded", counts["excluded"]],
                                               ["cases", cohort.get("n_cases", 0)], ["controls", cohort.get("n_controls", 0)], ["unassigned", cohort.get("n_unassigned", 0)],
                                               ["scores", ", ".join(self.cohort.get("slnm") or {}) or "none"]])]
        if reasons:
            parts.append("<h3>Exclusions</h3>" + _list(f"{_e(reason)}: {_e(', '.join(sids))}" for reason, sids in reasons.items()))
        overrides = []
        for item in self.data.get("overrides") or []:
            who = f" ({item['participant_id']})" if item.get("participant_id") else ""
            overrides.append(f'<span class="amber">{_e(item.get("what"))}{_e(who)}: {_e(item.get("reason"))}</span>')
        if self.config.get("volume_off_reason") and not any(item.get("what") == "no_volume_covariate" for item in self.data.get("overrides") or []):
            overrides.append(f'<span class="amber">volume covariate off: {_e(self.config["volume_off_reason"])}</span>')
        invalid = [sid for reason, sids in reasons.items() if "empty" in reason or "invalid" in reason or "outside" in reason for sid in sids]
        if invalid:
            overrides.append(f'<span class="amber">invalid lesions excluded: {_e(", ".join(invalid))}</span>')
        parts.append("<h3>Overrides</h3>" + (_list(overrides) or '<p class="mut">none recorded</p>'))
        parts.append("<h3>Thresholds</h3>" + self.thresholds_table())
        stages = []
        for stage in self.data.get("stages") or []:
            status = stage.get("status")
            css = "red" if status in ("failed", "blocked", "error") else "amber" if status in ("unavailable",) else ""
            stages.append([stage.get("label") or stage.get("name"), _td(_e(status) + (" (reused)" if stage.get("reused") else ""), css), stage.get("provider") or "built in",
                           _duration(stage.get("elapsed")), stage.get("reason") or ""])
        parts.append("<h3>Stages</h3>" + _table(["Stage", "Status", "Provider", "Time", "Note"], stages))
        warnings = self.data.get("warnings") or []
        if warnings:
            parts.append("<h3>Warnings</h3>" + _list(_e(text) for text in warnings))
        failed = self.data.get("failed_stages") or []
        parts.append("<h3>Failed stages</h3>" + (f'<p class="red">{_e(", ".join(failed))}</p>' if failed else '<p class="mut">none</p>'))
        return self.section("summary", "Summary", "".join(parts))

    def rule_fraction(self, tag):
        """Median over the mapped lesions of the fraction of in-mask voxels above the rule (both signs), from the recorded counts."""
        counts = self.cohort.get("per_lesion_counts") or {}
        v = self.connectome.get("n_voxels")
        values = sorted((item.get(f"{tag}Pos", 0) + item.get(f"{tag}Neg", 0)) / float(v) for item in counts.values() if isinstance(item, dict) and f"{tag}Pos" in item) if v else []
        if not values:
            return None
        middle = len(values) // 2
        return values[middle] if len(values) % 2 else 0.5 * (values[middle - 1] + values[middle])

    def resolved(self, entry):
        from . import thresholds

        try:
            return thresholds.Resolved.from_dict(entry.get("resolved"))
        except (ValueError, TypeError):
            return None

    def thresholds_table(self):
        from . import thresholds

        rows = []
        for entry in self.data.get("thresholds") or []:
            resolved = self.resolved(entry)
            fraction = self.rule_fraction(entry.get("tag"))
            text = thresholds.describe(resolved) if resolved else entry.get("derived") or "not recorded"
            flag = "refused: " + str(entry["refused"]) if entry.get("refused") else ("banner: " + resolved.banner if resolved and resolved.banner else "allowed" if entry.get("allowed") else "used anyway")
            css = "amber" if entry.get("refused") or (resolved and resolved.banner) or not entry.get("allowed", True) else ""
            rows.append([entry.get("tag"), ("primary" if entry.get("primary") else "secondary"), thresholds.rule_label(entry.get("rule")) if entry.get("rule") else "", text,
                         f"{_pct(fraction)} (median over lesions)" if fraction is not None else "n/a", _td(_e(flag), css)])
        if not rows:
            return '<p class="mut">No threshold recorded.</p>'
        return _table(["Tag", "Role", "Rule", "Derived threshold", "% of in-mask voxels above the rule", "Status"], rows)

    # 3. preflight

    def preflight(self):
        state = (_read_json(self.path(".pipeline", "stages", "preflight.json")) or {}).get("result") or {}
        report = _read_json(os.path.join(self.study_path, state["report"])) if self.study_path and state.get("report") else None
        if not isinstance(report, dict):
            counts = state.get("counts")
            text = ("Checks: " + ", ".join(f"{key} {value}" for key, value in counts.items())) if counts else "not recorded (the preflight report is not readable)"
            return self.section("preflight", "Preflight", f'<p class="mut">{_e(text)}</p>')
        rows = []
        for check in report.get("checks") or []:
            level = check.get("level")
            css = "red" if level == "block" else "amber" if level == "warn" else ""
            fix = (check.get("fix") or {}).get("label") or ""
            rows.append([_td(_e(level), css), check.get("message"), len(check.get("subjects") or []), "yes" if check.get("excludes") else "no", fix])
        counts = report.get("counts") or {}
        head = f'<p>{_e(", ".join(f"{key}: {value}" for key, value in counts.items()))}</p>'
        return self.section("preflight", "Preflight", head + (_table(["Level", "Check", "Subjects", "Excludes", "Fix"], rows) if rows else '<p class="mut">no finding</p>'))

    # 4. lesions

    def lesions(self):
        from . import reportfigs

        parts = []
        path = self.group_path("lesionCount")
        overlap = self.lesionset.get("overlap") or {}
        if path:
            import numpy as np

            from . import nifti

            data, _affine, _header = nifti.read_nifti(path)
            vmax = int(np.nanmax(data)) if np.size(data) else 1
            legend = overlap.get("legend") or f"max {vmax}"
            caption = f"Lesion overlap: number of lesions covering each voxel, exact integer bins 1–{max(vmax, 1)}; {legend}."  # noqa: RUF001
            parts.append(self.montage_html(path, "sequential", caption, vmax=max(vmax, 1), alt="Lesion overlap montage",
                                           bar=self.colorbar("sequential", 1, max(vmax, 1), "lesions per voxel", vmax=max(vmax, 1))))
        else:
            parts.append('<p class="mut">Lesion overlap: not computed (no lesionCount map).</p>')
        lesions = self.lesionset.get("lesions") or []
        volumes = [item.get("volume_ml") for item in lesions]
        if any(value is not None for value in volumes):
            parts.append("<figure>" + reportfigs.dot_plot_svg(volumes, labels=[f"{item.get('participant_id')}: {item.get('volume_ml')} mL" for item in lesions], title="Lesion volumes (mL)", unit="mL")
                         + "<figcaption>Lesion volumes (mL), one dot per lesion (fixed jitter).</figcaption></figure>")
        coverage = []
        flagged = 0
        for row in self.mapped:
            lesion = (self.sidecars.get(row["participant_id"]) or {}).get("lesion") or {}
            outside = lesion.get("outside_connectome_fraction")
            flag = outside is not None and outside > COVERAGE_FLAG
            flagged += flag
            coverage.append([row["participant_id"], row.get("group") or "", _num(lesion.get("voxels_on_connectome_grid")), _td(_e(_pct(outside)) + (" (flagged)" if flag else ""), "amber" if flag else "")])
        if coverage:
            parts.append(f"<h3>Coverage</h3><p>{flagged} lesion(s) have more than {COVERAGE_FLAG:.0%} of their weight outside the connectome mask (amber).</p>"
                         + "<details><summary>Coverage table</summary>" + _table(["Lesion", "Group", "Voxels on the connectome grid", "Weight outside the mask"], coverage) + "</details>")
        parts.append(self.lesionset_html())
        parts.append(self.normalization_html())
        return self.section("lesions", "Lesions", "".join(parts))

    def lesionset_html(self):
        summary = self.lesionset
        if not summary:
            return '<h3>Lesion-set diagnostics</h3><p class="mut">not run</p>'
        dice = summary.get("dice") or {}
        volumes = summary.get("volumes") or {}
        rows = [["lesions", summary.get("n")], ["hemisphere", ", ".join(f"{key} {value}" for key, value in (summary.get("hemisphere") or {}).items())],
                ["pairwise Dice (mean / median / max)", f"{_num(dice.get('mean'))} / {_num(dice.get('median'))} / {_num(dice.get('max'))}"],
                ["volume mL (median, IQR)", f"{_num(volumes.get('median'))} ({_num(volumes.get('q1'))}–{_num(volumes.get('q3'))})"],  # noqa: RUF001
                ["dimensionality", ", ".join(f"{key} {value}" for key, value in (summary.get("dimensionality") or {}).items())]]
        for key, item in (summary.get("balance") or {}).items():
            if isinstance(item, dict) and item.get("flagged"):
                rows.append([_td(_e(f"imbalance: {key}"), "amber"), f"SMD {_num(item.get('smd'))}; cases {_num(item.get('case_mean'))}, controls {_num(item.get('control_mean'))}"])
        literature = [_e(item.get("text")) for item in summary.get("literature") or [] if isinstance(item, dict)]
        return "<h3>Lesion-set diagnostics</h3>" + _table(["Diagnostic", "Value"], rows) + ("<p>Literature context:</p>" + _list(literature) if literature else "")

    def normalization_html(self):
        participants = _read_tsv(os.path.join(self.study_path, "participants.tsv")) if self.study_path else None
        mapped = {row["participant_id"] for row in self.mapped}
        rows = []
        for row in participants or []:
            if row.get("participant_id") in mapped and any(row.get(key) not in (None, "", "n/a") for key in ("norm_method", "norm_qc")):
                qc = row.get("norm_qc") or "not recorded"
                rows.append([row["participant_id"], row.get("norm_method") or "not recorded", _td(_e(qc), "" if qc == "pass" else "amber")])
        if not rows:
            return '<h3>Normalized lesions</h3><p class="mut">none (every mapped lesion was traced in template space)</p>'
        return "<h3>Normalized lesions</h3>" + _table(["Lesion", "Normalization method", "Normalization QC"], rows)

    # 5. networks

    def networks(self):
        entries = self.data.get("thresholds") or []
        if not entries:
            return [self.section("network", "Network", '<p class="mut">not computed: no threshold rule was recorded</p>')]
        out = []
        for entry in entries:
            out.append(self.network(entry))
        return out

    def network(self, entry):
        tag = entry.get("tag") or "rule"
        primary = bool(entry.get("primary"))
        resolved = self.resolved(entry)
        from . import thresholds

        title = f"Network ({tag}{', primary rule' if primary else ''})"
        if entry.get("refused"):
            return self.section(f"network-{tag}", title, f'<p class="amber">Rule refused: {_e(entry["refused"])}</p>', details=None if primary else False)
        parts = [f"<p>{_e(thresholds.rule_label(entry['rule']))}: {_e(thresholds.describe(resolved) if resolved else entry.get('derived'))}</p>"]
        mean = self.group_path("meanT")
        threshold = resolved.value if resolved and resolved.statistic == "T" and math.isfinite(resolved.value) and resolved.rule.get("kind") != "top_pct" else None
        sign = (entry.get("rule") or {}).get("sign", "both")
        if mean:
            note = "" if threshold is not None else " Shown unthresholded: this rule does not give one |T| for the mean map."
            side = self.group_sidecar("meanT")
            parts.append(self.montage_html(mean, "diverging", (f"meanT: {MEAN_CAPTION}; n = {side.get('n_maps', 'n/a')} maps ({side.get('population', 'not recorded')}); voxels with |T| below {_num(threshold)} are hidden.{note}" if threshold else f"meanT: {MEAN_CAPTION}.{note}"),
                                           threshold=threshold, sign=sign, alt=f"Mean T montage ({tag})"))
        for suffix, label in (("Pos", "positive network, headline"), ("Neg", "negative network, secondary")):
            desc = self.desc_for("percent", suffix.lower(), tag, primary)
            path = self.group_path(desc) if desc else None
            if path:
                side = self.group_sidecar(desc)
                caption = f"overlapPct{suffix} ({label}): % of the {side.get('n_maps', 'n/a')} lesion network maps ({side.get('population', 'not recorded')}) above the rule; classic overlap, hub-inflated (read it next to the null models)."
                parts.append(self.montage_html(path, "sequential", caption, vmax=100.0, alt=f"Overlap percent {suffix} montage ({tag})",
                                               bar=self.colorbar("sequential", 0, 100, "% of maps above the rule")))
        pos, neg = self.desc_for("binary", "pos", tag, primary), self.desc_for("binary", "neg", tag, primary)
        if pos and self.group_path(pos):
            from . import colormaps

            overlays = [(self.group_path(neg), "binary", colormaps.NEGATIVE_COLORS[0])] if neg and self.group_path(neg) else []
            k = self.config.get("sensitivity")
            caption = f"Sensitivity: voxels where at least {_num(k)}% of the maps pass the rule; positive in red, negative in blue (binary)."
            parts.append(self.montage_html(self.group_path(pos), "binary", caption, color=colormaps.POSITIVE_COLORS[0], overlays=overlays, alt=f"Sensitivity montage ({tag})", bar=""))
        counts = self.cohort.get("per_lesion_counts") or {}
        v = self.connectome.get("n_voxels")
        rows = [[sid, _num(item.get(f"{tag}Pos")), _num(item.get(f"{tag}Neg")), _pct((item.get(f"{tag}Pos", 0) + item.get(f"{tag}Neg", 0)) / v) if v else "n/a"] for sid, item in counts.items() if f"{tag}Pos" in item]
        if not (mean or len(parts) > 1):
            parts.append(self.not_run("overlap", "Network maps not computed"))
        if rows:
            parts.append("<details><summary>Per-lesion suprathreshold voxels</summary>" + _table(["Lesion", "Positive", "Negative", "% of in-mask voxels"], rows) + "</details>")
        return self.section(f"network-{tag}", title, "".join(parts), details=None if primary else False)

    def desc_for(self, kind, sign, tag, primary):
        for desc, item in self.groups.items():
            if item.get("kind") == kind and item.get("sign") == sign and item.get("rule_tag") == tag and (desc.startswith("overlapPct") if kind == "percent" else desc.startswith("sens")):
                return desc
        return None

    def stage(self, name):
        return next((stage for stage in self.data.get("stages") or [] if stage.get("name") == name), {})

    def not_run(self, name, prefix="not run"):
        stage = self.stage(name)
        reason = stage.get("reason") or stage.get("status") or "not recorded"
        estimate = f" (estimated {_duration(stage['estimate_s'])})" if stage.get("estimate_s") else ""
        return f'<p class="mut">{_e(prefix)}: {_e(reason)}{_e(estimate)}</p>'

    def masked(self, values_path, p_path, sign=None):
        """(data, affine) of a map with the voxels whose recorded p is not below alpha (and of the other sign) set to 0."""
        import numpy as np

        from . import nifti

        data, affine, _header = nifti.read_nifti(values_path)
        p, _p_affine, _p_header = nifti.read_nifti(p_path)
        data = np.asarray(data, dtype=np.float32)
        keep = np.isfinite(p) & (np.asarray(p) < self.alpha)
        if sign == "pos":
            keep &= data > 0
        elif sign == "neg":
            keep &= data < 0
        return np.where(keep, data, 0.0).astype(np.float32), affine, int(np.count_nonzero(keep))

    # 6. nulls

    def nulls(self):
        from . import methods

        info = self.cohort.get("nulls")
        if not info:
            return self.section("nulls", "Null models", self.not_run("nulls"))
        parts = [(f"<p>{_e(info.get('mode_label'))} ({_e(info.get('kind'))}{', approximate' if info.get('approximate') else ''}); K = {_num(info.get('k'))} null cohorts; "
                 f"minimum attainable p = {_num(info.get('min_attainable_p'))}, q = {_num(info.get('min_attainable_q'))}; BH-FDR needs at least {_num(info.get('fdr_floor_voxels'))} voxels.</p>")]
        classic = info.get("classic_rule") or {}
        if classic:
            parts.append(f"<p>Classic rule false-positive rate: {_e(classic.get('text'))}. <span class=\"mut\">{_e(classic.get('false_positive_rate_note'))}</span></p>")
        excess, pfwe = self.group_path("sensNullExcess"), self.group_path("sensNullPFwe")
        if excess and pfwe:
            values, affine, kept = self.masked(excess, pfwe)
            if kept:
                parts.append(self.montage_html((values, affine, "sensNullExcess"), "diverging", (f"Null-referenced sensitivity: observed minus null overlap (percentage points), shown where PFWE < {self.alpha:g} ({kept} voxels). {info.get('caption') or methods.NULL_CAPTION}"),
                                               threshold=None, alt="Null-referenced sensitivity montage"))
            else:
                parts.append(f"<p>No voxel of the null-referenced sensitivity reaches PFWE &lt; {self.alpha:g}.</p>")
        group_qa = info.get("group_qa") or {}
        if group_qa.get("text"):
            parts.append(f"<p>Group QA against null cohorts: {_e(group_qa.get('text'))}</p>")
        parts.append(f'<p class="mut">{_e(info.get("caption") or methods.NULL_CAPTION)}</p>')
        return self.section("nulls", "Null models", "".join(parts))

    # 7. specificity

    def specificity(self):
        spec = self.cohort.get("specificity")
        stage = self.stage("specificity")
        if not spec or stage.get("status") not in ("done", "reused"):
            text = self.not_run("specificity", "not computed")
            if str(self.config.get("specificity", "")).startswith("legacy"):
                text += '<p class="mut">The requested route is labelled "legacy two-group, no covariates".</p>'
            return self.section("specificity", "Specificity (GLM)", text)
        design = ((_read_json(self.path("stages", "glm", "design.json")) or {}).get("resolved")) or {}
        record = _read_json(self.path("stages", "glm", "glm.json")) or {}
        permutation = record.get("permutation") or {}
        fdr = record.get("fdr") or {}
        rows = [["design type", spec.get("design_type") or design.get("design_type")], ["test", record.get("test") or design.get("test")],
                ["contrast", spec.get("label")], ["columns", ", ".join(design.get("columns") or [])], ["covariates", ", ".join(record.get("covariates") or design.get("covariates") or []) or "none"],
                ["exchangeability", f"{permutation.get('scheme') or design.get('permute')}: {permutation.get('reason') or design.get('permute_reason') or ''}"],
                ["permutations", f"{_num(permutation.get('n_perm') or design.get('n_perm'))} ({'exhaustive' if permutation.get('exhaustive') or design.get('exhaustive') else 'random'}; {permutation.get('method') or 'not recorded'})"],
                ["minimum attainable p / q", f"{_num(record.get('min_attainable_p') or spec.get('min_p'))} / {_num(fdr.get('min_attainable_q'))}"],
                ["n (per level)", f"{_num(spec.get('n'))} ({', '.join(f'{k} {v}' for k, v in (record.get('n_per_level') or {}).items())})"],
                ["limitations", "; ".join(spec.get("limitations") or record.get("limitations") or []) or "none recorded"]]
        parts = [_table(["Design", "Value"], rows)]
        label = spec.get("label") or "contrast"
        tstat, pfwe = self.group_path(f"{label}Tstat"), self.group_path(f"{label}PFwe")
        fwe = spec.get("fwe") or {}
        if tstat and pfwe:
            for sign, role in (("pos", "positive (headline)"), ("neg", "negative (secondary)")):
                values, affine, kept = self.masked(tstat, pfwe, sign)
                count = fwe.get(sign) or {}
                if kept:
                    caption = f"GLM {label} T statistic, {role}: voxels with PFWE < {self.alpha:g} ({_num(count.get('voxels', kept))} voxels, {_num(count.get('ml'))} mL; permutation max-statistic FWE)."
                    parts.append(self.montage_html((values, affine, f"{label}Tstat{sign}"), "diverging", caption, sign=sign, alt=f"GLM T {sign} montage"))
                else:
                    parts.append(f"<p>{_e(role.capitalize())}: no voxel survives FWE (PFWE &lt; {self.alpha:g}).</p>")
        pfdr = self.group_path(f"{label}PFdr")
        secondary = []
        if fdr:
            secondary.append(f"FDR (secondary): {_num(fdr.get('n_significant'))} voxels with q < {self.alpha:g}. {fdr.get('note') or ''}")
        effect = self.group_path(f"{label}HedgesG")
        if effect:
            secondary.append("Hedges g map: " + self.rel_link(os.path.basename(effect)))
        if pfdr:
            secondary.append("FDR q map: " + self.rel_link(os.path.basename(pfdr)))
        parts.append(_list(item if "<a " in item else _e(item) for item in secondary))
        return self.section("specificity", "Specificity (GLM)", "".join(parts))

    # 8. conjunction

    def conjunction(self):
        conj = self.cohort.get("conjunction")
        pos, neg = self.group_path("conjPos"), self.group_path("conjNeg")
        if not conj or not (pos or neg):
            return self.section("conjunction", "Conjunction", self.not_run("conjunction", "not computed"))
        from . import colormaps

        parts = [(f"<p>Conjunction of sensitivity and specificity (sens AND sign(t) AND PFWE &lt; {self.alpha:g}): positive {_num(conj.get('pos_voxels'))} voxels ({_num(conj.get('pos_ml'))} mL), "
                 f"negative {_num(conj.get('neg_voxels'))} voxels ({_num(conj.get('neg_ml'))} mL).</p>")]
        base = pos or neg
        overlays = [(neg, "binary", colormaps.NEGATIVE_COLORS[0])] if pos and neg else []
        parts.append(self.montage_html(base, "binary", "Conjunction map: positive in red, negative in blue (binary).", color=colormaps.POSITIVE_COLORS[0] if pos else colormaps.NEGATIVE_COLORS[0], overlays=overlays, alt="Conjunction montage", bar=""))
        peak = self.largest_cluster_peak(("conjPos", "conjNeg"))
        if peak is not None and self.canvas is not None:
            desc, point = peak
            result = self.canvas.ortho(self.group_path(desc), point, kind="binary", color=colormaps.POSITIVE_COLORS[0] if desc == "conjPos" else colormaps.NEGATIVE_COLORS[0],
                                       alt=f"Orthogonal views of {desc} through the peak of its largest cluster")
            parts.append(self.figure(result, _e(f"Orthogonal views through the peak of the largest {desc} cluster at ({', '.join(f'{c:+g}' for c in point)}) mm (crosshair). {self.orientation_text()}")))
        return self.section("conjunction", "Conjunction", "".join(parts))

    def largest_cluster_peak(self, descs):
        best = None
        for desc in descs:
            item = (self.outputs.get("clusters") or {}).get(desc) or {}
            rows = _read_tsv(self.path(item["tsv"])) if item.get("tsv") else None
            for row in rows or []:
                try:
                    voxels = int(row.get("voxels") or 0)
                    point = (float(row["x"]), float(row["y"]), float(row["z"]))
                except (TypeError, ValueError, KeyError):
                    continue
                if voxels and (best is None or voxels > best[0]):
                    best = (voxels, desc, point)
        return None if best is None else best[1:]

    # 9. sLNM

    def slnm(self):
        scores = self.cohort.get("slnm") or {}
        if not scores:
            return [self.section("slnm", "Symptom network maps (sLNM)", self.not_run("slnm", "not computed"))]
        out = []
        for label, info in scores.items():
            parts = []
            if info.get("skipped") or info.get("failed"):
                parts.append(f'<p class="mut">not computed: {_e(info.get("skipped") or info.get("failed"))}</p>')
                out.append(self.section(f"slnm-{label}", f"sLNM: {label}", "".join(parts)))
                continue
            parts.append(f"<p>Score {_e(info.get('column'))}: n = {_num(info.get('n'))}, method {_e(info.get('method'))}, covariates {_e(', '.join(info.get('covariates') or []) or 'none')}; "
                         f"r(score, lesion volume) = {_num(info.get('r_with_volume'))}. Inference: score permutation, max-statistic FWE.</p>")
            r_path, p_path = self.group_path(f"slnm{label}R"), self.group_path(f"slnm{label}PFwe")
            if r_path and p_path:
                values, affine, kept = self.masked(r_path, p_path)
                if kept:
                    caption = f"Partial r of {info.get('column')} with the network maps, shown where PFWE < {self.alpha:g} ({kept} voxels): a network associated with the score."
                    parts.append(self.montage_html((values, affine, f"slnm{label}R"), "diverging", caption, alt=f"sLNM {label} montage"))
                else:
                    parts.append(f"<p>No voxel survives FWE (PFWE &lt; {self.alpha:g}); uncorrected maps are not shown as results.</p>")
            out.append(self.section(f"slnm-{label}", f"sLNM: {label}", "".join(parts)))
        return out

    # 10. QA

    def qa_section(self):
        from . import qa as qalib
        from . import reportfigs

        lesions = self.qa.get("lesions") or {}
        if not lesions:
            return self.section("qa", "Connectome-confound QA", self.not_run("qa", "not available"))
        rows, percentiles, names = [], [], []
        for sid, item in lesions.items():
            reference = item.get("reference") if isinstance(item.get("reference"), dict) else None
            if reference:
                level, text = qalib.calibrated_level(reference.get("percentile"), reference.get("k") or 0)
                css = {"below": "", "amber": "amber", "red": "red"}.get(level, "")
                chip = f'<span class="chip {css}">{_e(text)}</span> ' + reportfigs.percentile_strip_svg(reference.get("percentile"), reference.get("k"), title=f"{sid}: percentile {_num(reference.get('percentile'))}")
                similarity = f"μC_r r = {_num(reference.get('mu_r'))}; PC1 r = {_num(reference.get('pc1_r'))}; partial (PC1 | μC) = {_num(reference.get('partial_pc1_mu'))}"
                percentiles.append(reference.get("percentile"))
                names.append(sid)
            else:
                chip = '<span class="chip">not available</span>'
                similarity = "n/a"
            secondary = []
            if item.get("degree_r") is not None:
                secondary.append(f"degree r = {_num(item['degree_r'])} ({qalib.chip_level(item['degree_r'])[1]}; fixed 0.3/0.6 cut-offs)")
            null = item.get("null") or {}
            if null.get("r_mean_map") is not None:
                secondary.append(f"r with the mean relocated map = {_num(null['r_mean_map'])} ({'≥' if null['r_mean_map'] >= qalib.NULL_AMBER else '<'} {qalib.NULL_AMBER:g} fixed cut-off; K = {_num(null.get('k'))})")
            rows.append([sid, _td(chip), similarity, _td(f'<span class="mut">{_e("; ".join(secondary) or "none")}</span>')])
        parts = []
        if percentiles:
            parts.append("<figure>" + reportfigs.dot_plot_svg(percentiles, bands=[(0, qalib.CALIBRATED_LOW, "band-low"), (qalib.CALIBRATED_LOW, qalib.CALIBRATED_HIGH, "band-amber"), (qalib.CALIBRATED_HIGH, 100, "band-red")],
                                                              labels=[f"{sid}: {_num(p)}" for sid, p in zip(names, percentiles, strict=False)], title="Percentile of r(map, μC_r) among relocated lesions", xmin=0, xmax=100, unit="percentile")
                         + "<figcaption>Percentile of each map's similarity to μC_r among its relocated lesions (below 5th, 5th–95th amber, above 95th red).</figcaption></figure>")  # noqa: RUF001
        else:
            parts.append(f"<p>{_e(NOT_AVAILABLE_QA)}.</p>")
        parts.append(_table(["Lesion", "Calibrated chip", "Similarity", "Fixed cut-offs (secondary)"], rows))
        group_rows = []
        nulls = self.cohort.get("nulls") or {}
        if (nulls.get("group_qa") or {}).get("text"):
            group_rows.append(["sensitivity (null cohorts)", nulls["group_qa"]["text"]])
        summary = self.qa.get("summary") or {}
        group_rows.append(["lesions", f"{_num(summary.get('n'))} maps; amber {_num(summary.get('n_amber'))}, red {_num(summary.get('n_red'))}; calibrated: {'yes' if summary.get('calibrated') else 'no'}"])
        parts.append("<h3>Group maps</h3>" + _table(["Map", "QA"], group_rows))
        parts.append(f"<p>{_e(DEBATE_PARAGRAPH)}</p>")
        parts.append("<p class=\"mut\">References: " + "; ".join(f'{_e(text)}, <a href="{_e(DOI_BASE + doi)}">doi:{_e(doi)}</a>' for text, doi in REFERENCES) + ".</p>")
        return self.section("qa", "Connectome-confound QA", "".join(parts))

    # 11. clusters

    def clusters(self):
        tables = self.outputs.get("clusters") or {}
        parts = []
        for desc, item in tables.items():
            if desc == "peaks" or not isinstance(item, dict) or not item.get("tsv"):
                continue
            rows = _read_tsv(self.path(item["tsv"]))
            if rows is None:
                continue
            points = []
            for row in rows:
                try:
                    points.append((float(row["x"]), float(row["y"]), float(row["z"])))
                except (TypeError, ValueError, KeyError):
                    points.append(None)
            labels = self.point_labels(points, rows)
            body = []
            for row, (label, network) in zip(rows, labels, strict=False):
                body.append([row.get("cluster_id"), row.get("sign"), row.get("x"), row.get("y"), row.get("z"), row.get("peak"), row.get("volume_mm3") or "", label, network])
            caption = f"{desc}: {len(rows)} row(s) at threshold {_num(item.get('threshold'))}; {CLUSTER_NOTE}."
            parts.append(f"<h3>{_e(desc)}</h3><p class=\"mut\">{_e(caption)} {self.rel_link(item['tsv'], 'TSV')}</p>"
                         + (_table(["Cluster", "Sign", "x (MNI mm)", "y", "z", "Value", "Extent (mm³)", "Atlas label", "Network"], body) if body else '<p class="mut">no cluster</p>'))
        peaks = _read_tsv(self.path("peaks.tsv"))
        if peaks:
            points = []
            for row in peaks:
                try:
                    points.append((float(row["x"]), float(row["y"]), float(row["z"])))
                except (TypeError, ValueError, KeyError):
                    points.append(None)
            labels = self.point_labels(points, peaks, column="label")
            source = ((tables.get("peaks") or {}).get("source")) or "headline map"
            parts.append(f"<h3>Peaks of {_e(source)}</h3>" + _table(["x (MNI mm)", "y", "z", "Value", "Atlas label", "Network"], [[row.get("x"), row.get("y"), row.get("z"), row.get("value"), label, network] for row, (label, network) in zip(peaks, labels, strict=False)]))
        if not parts:
            return self.section("clusters", "Clusters and peaks", self.not_run("clusters", "not computed"))
        notes = "".join(f'<p class="amber">{_e(note)}</p>' for note in self.notes)
        return self.section("clusters", "Clusters and peaks", f"<p>{_e(CLUSTER_NOTE.capitalize())}.</p>" + notes + "".join(parts))

    def point_labels(self, points, rows, column="labels"):
        if self.atlas() is not None:
            valid = [point for point in points if point is not None]
            labelled = iter(self.label_points(valid))
            return [next(labelled) if point is not None else ("n/a", "") for point in points]
        out = []
        for row in rows:
            text = (row.get(column) or "").strip()
            out.append((text if text and text != "n/a" else "n/a", (row.get("network") or "").strip()))
        return out

    # 12. gallery

    def gallery_order(self):
        order = self.lesionset.get("gallery") or self.lesionset.get("order")
        if isinstance(order, dict):
            order = order.get("sids") or order.get("order")
        ids = [row["participant_id"] for row in self.mapped]
        if isinstance(order, list) and order:
            first = [sid for sid in order if sid in ids]
            return first + [sid for sid in ids if sid not in first]
        return ids

    def strip_window(self):
        if self._window is None:
            from . import nifti, reportfigs

            mean = self.group_path("meanT")
            entry = next((e for e in self.data.get("thresholds") or [] if e.get("primary")), None)
            resolved = self.resolved(entry) if entry else None
            threshold = resolved.value if resolved and math.isfinite(resolved.value) else None
            if mean:
                data, _affine, _header = nifti.read_nifti(mean)
                self._window = (reportfigs.window_of(data, threshold), threshold, resolved)
            else:
                self._window = (max(1.0, (threshold or 1.0) * 1.1), threshold, resolved)
        return self._window

    def gallery(self):
        order = self.gallery_order()
        if not order:
            return self.section("gallery", "Lesion gallery", '<p class="mut">no mapped lesion</p>')
        window, threshold, resolved = self.strip_window()
        by_id = {row["participant_id"]: row for row in self.mapped}
        volumes = {item.get("participant_id"): item.get("volume_ml") for item in self.lesionset.get("lesions") or [] if isinstance(item, dict)}
        lesions_qa = self.qa.get("lesions") or {}
        figures_html = []
        for number, sid in enumerate(order[:GALLERY_MAX], start=1):
            row = by_id[sid]
            lesion = self._resolve(row.get("snapshot")) if row.get("snapshot") else None
            sidecar = self.sidecars.get(sid) or {}
            t_rel = (sidecar.get("maps") or {}).get("T")
            t_map = self.path(t_rel) if t_rel and os.path.isfile(self.path(t_rel)) else None
            if not lesion or not os.path.isfile(lesion) or self.canvas is None:
                missing = "lesion snapshot not found" if not lesion or not os.path.isfile(lesion) else NO_DISPLAY_GRID
                figures_html.append(f"<figure><figcaption>#{number} {_e(sid)}: {missing}</figcaption></figure>")
                self.progress()
                continue
            from . import reportfigs

            result = self.canvas.gallery_strip(lesion, t_map, threshold if resolved is None or resolved.statistic == "T" else None, window, label=str(number))
            item = lesions_qa.get(sid) or {}
            reference = item.get("reference") if isinstance(item.get("reference"), dict) else None
            if reference:
                from . import qa as qalib

                level, _text = qalib.calibrated_level(reference.get("percentile"), reference.get("k") or 0)
                chip = f'<span class="chip {"red" if level == "red" else "amber" if level == "amber" else ""}">QA {_e(level)}</span>'
            else:
                chip = '<span class="chip">QA not calibrated</span>'
            ml = volumes.get(sid)
            if ml is None:
                mm3 = (sidecar.get("lesion") or {}).get("volume_mm3")
                ml = None if mm3 is None else mm3 / 1000.0
            caption = f"#{number} {_e(sid)} · {_e(row.get('group') or 'unassigned')} · {_e(_num(ml))} mL " + chip
            figures_html.append(self.figure(result, caption, colors=reportfigs.STRIP_COLORS))
        note = ""
        if len(order) > GALLERY_MAX:
            note = f'<p class="amber">The gallery shows the first {GALLERY_MAX} of {len(order)} lesions.</p>'
        intro = (f"<p>One strip per lesion in supplementary-table order: the lesion (red) on the axial slice through its centroid; its T map on that slice with the lesion outlined in white; "
                 f"the axial maximum-|T| projection over the template's maximum projection. Map values with |T| below {_num(threshold)} are hidden; colour range ±{_num(window)} shared by all strips. {_e(self.orientation_text())}</p>")
        content = intro + note + f'<details{" open" if len(order) <= GALLERY_OPEN_MAX else ""}><summary>{len(order[:GALLERY_MAX])} lesions</summary><div class="gal">' + "".join(figures_html) + "</div></details>"
        return self.section("gallery", "Lesion gallery", content)

    # 13. methods

    def methods(self):
        page = None
        try:
            with open(self.path("methods.html"), encoding="utf-8") as f:
                page = f.read()
        except OSError:
            page = None
        if page and '<section id="methods-' in page:
            body = page.split("<body>", 1)[-1].rsplit("</body>", 1)[0]
            body = re.sub(r"<h1>.*?</h1>", "", body, count=1, flags=re.DOTALL)
            body = body.replace("<h2>", "<h3>").replace("</h2>", "</h3>")
            return self.section("methods", "Methods", body)
        try:
            with open(self.path("methods.md"), encoding="utf-8") as f:
                return self.section("methods", "Methods", markdown_to_html(f.read()))
        except OSError:
            return self.section("methods", "Methods", '<p class="mut">not recorded (no methods file)</p>')

    # 14. checklist

    def checklist(self):
        from . import checklist as checklistlib

        items = (self.export_state().get("methods") or {}).get("items")
        rows = []
        if items:
            for item in items:
                status = item.get("status")
                css = "amber" if status == checklistlib.FLAGGED else "green" if status == checklistlib.DONE else ""
                rows.append([item.get("id"), item.get("title"), _td(_e(checklistlib.STATUS_LABELS.get(status, status)), css), item.get("value")])
        else:
            try:
                with open(self.path("checklist.md"), encoding="utf-8") as f:
                    lines = f.read().split("## Battery")[0].splitlines()
            except OSError:
                lines = []
            for line in lines:
                cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
                if len(cells) >= 4 and re.fullmatch(r"[A-K]\d+", cells[0]):
                    css = "amber" if cells[2] == "flagged" else "green" if cells[2] == "done" else ""
                    rows.append([cells[0], cells[1], _td(_e(cells[2]), css), cells[3]])
        if not rows:
            return self.section("checklist", "Reporting checklist", '<p class="mut">not recorded (the methods step did not run)</p>')
        return self.section("checklist", "Reporting checklist", _table(["Item", "Check", "Status", "Value"], rows) + '<p class="mut">Checklist and battery warnings never block an export.</p>')

    # 15. reproduce

    def reproduce(self):
        parts = []
        if os.path.isfile(self.path("reproduce_pipeline.sh")):
            parts.append("<p>Pipeline (from this folder; " + self.rel_link("reproduce_pipeline.sh") + "):</p><pre>ACCEPT_TERMS=1 sh reproduce_pipeline.sh</pre>")
        if os.path.isfile(self.path("reproduce.sh")):
            parts.append("<p>Network maps only (engine; " + self.rel_link("reproduce.sh") + "):</p><pre>sh reproduce.sh</pre>")
        parts.append("<p>Windows (cmd.exe), from this folder:</p><pre>" + _e('set PYTHONPATH=<LesionNetworkMapping module folder> && "C:\\path\\to\\PythonSlicer.exe" -m LesionNetworkMappingLib.pipeline run --config pipeline.json --out reproduced --recompute --accept-terms') + "</pre>")
        parts.append("<p>The connectome's data use terms must be accepted explicitly: ACCEPT_TERMS=1 (or --accept-terms) is needed; without it the run stops with exit code 4.</p>")
        versions = self.data.get("versions") or {}
        host = self.data.get("host") or {}
        parts.append("<p>Versions: " + _e(", ".join(f"{key} {value}" for key, value in versions.items())) + ". Host: " + _e(", ".join(f"{key} {value}" for key, value in host.items())) + ".</p>")
        parts.append("<h3>Configuration</h3><pre>" + _e(json.dumps(self.config, indent=2, ensure_ascii=False, sort_keys=True)) + "</pre>")
        return self.section("reproduce", "Reproduce", "".join(parts))

    # 16. files

    def files(self):
        rows = []
        for root, directories, names in os.walk(self.out):
            directories[:] = sorted(d for d in directories if d not in FILES_PRIVATE)
            for name in sorted(names):
                rel = os.path.relpath(os.path.join(root, name), self.out)
                if rel == REPORT_NAME or name.endswith(".tmp") or name.startswith(".report-"):
                    continue
                try:
                    size = os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
                rows.append([_td(self.rel_link(rel)), _size(size)])
        rows.sort(key=lambda row: row[0])
        return self.section("files", "Files", f"<p>{len(rows)} files (paths relative to this report).</p><details><summary>File list</summary>" + _table(["File", "Size"], rows) + "</details>")


def _size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size} B"


def render_report(out_dir, emit=None, cancel=None, atlas=None, atlas_labels=None, template=None, convention=None, now=None, strict=False):
    """Write ``<out_dir>/report.html`` from a pipeline output folder and return its path.

    :param emit: ``emit(event, **fields)`` (the pipeline's Emitter or ``jobs.emit``): a progress event
      {stage: "report", done, total} after each figure and warning events.
    :param cancel: None, a callable returning True to stop, or an object with ``raise_if_cancelled``/``is_set``;
      checked between figures (``jobs.Cancelled`` is raised).
    :param atlas: atlas NIfTI path or cached catalog id (labels of cluster peaks; never downloaded), with
      ``atlas_labels`` its label table (TSV); default: the run's configuration, else the cluster tables' labels.
    :param template: display template (default: the run's configuration, else :func:`reportfigs.find_template`).
    :param convention: "neurological" (default) or "radiological".
    :param now: the "Generated" timestamp (default: now, UTC); the only run-specific line of the file.
    :param strict: raise ValueError when the page uses banned wording (tests); otherwise a warning event.
    :raises ReportError: no pipeline.json of format slicer-lnm-pipeline in ``out_dir``.
    """
    now = now or datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    report = _Report(out_dir, emit, cancel, atlas, atlas_labels, template, convention, now)
    page = report.build()
    violations = lint_wording(page)
    if violations:
        message = f"report.html uses wording the reporting rules avoid: {', '.join(sorted(set(violations)))}"
        if strict:
            raise ValueError(message)
        report.warn(message, code="wording")
    path = report.path(REPORT_NAME)
    tmp = report.path(f".report-{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    os.replace(tmp, path)
    return path

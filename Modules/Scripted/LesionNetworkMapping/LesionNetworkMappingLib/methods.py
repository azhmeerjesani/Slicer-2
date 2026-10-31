"""Sectioned methods text generated from what was actually run (reporting v2; standard library, plus
numpy through .qa for the QA section).

:func:`methods_sections` reads a study (``participants.tsv``, ``study.json``), its run registry
(``derivatives/lnm/runs.jsonl``, see :mod:`.runs`) and the files the runs wrote (cohortmaps.json,
provenance.json, qa.json, glm.json, slnm.json, normalization.json, pipeline.json, ...) and returns
an ordered list of :class:`Section`::

    pipeline        stages with their providers, reused or skipped stages with reasons, overrides, exclusions
    lesions         lesion intake: sources, PRISMA counts, template, figure fits, L/R convention,
                    dimensionality and the 2D extrusion policy, mirroring, raters and review,
                    inter-rater agreement, normalization (BRAINSFit) and its QC, assisted and automatic tracing
    seeds           seed definition and the map statistic
    connectome      name, DOI, version, N, subject-list sha256, subset, GSR, preprocessing URL, mask sha256
    statistics      per run kind of the registry: GLM, sLNM, nulls, validation, robustness, ...
    thresholds      every recorded rule with its resolved value and p equivalent (thresholds.describe)
    qa              the reference definition (lnm-reference-v1) and the calibrated percentiles
    deviations      analysis_plan_deviations.jsonl
    limitations     the limitations written by the group analyses
    software        3D Slicer (RRID:SCR_005619), the fork revision, this library, Python, numpy, scipy
    citations, acknowledgements (GSP-DUT-2014 bundles only, or a bundle's own wording), disclaimer

Missing information is written as "not recorded" (:data:`NOT_RECORDED`), never dropped; analyses
that were not run are written as "not run". Wording follows addendum §3.11: results are a "network
associated with" a contrast or score, and :data:`BANNED_PHRASES` (shared with the report's wording
lint, :func:`lint_wording`) never appear in generated text. :func:`render` writes Markdown, HTML
(a fragment, or a standalone page) or plain text.

:func:`run_sections` gives the sections of one exported network-mapping run (``export.methods_text``).
"""

import dataclasses
import glob
import html
import json
import math
import os
import re
import statistics

from . import export

NOT_RECORDED = export.NOT_RECORDED
NOT_RUN = "not run"
SLICER_RRID = "RRID:SCR_005619"
#: Phrases that generated methods and reports never contain (addendum §3.11; used by the report's lint)
BANNED_PHRASES = ("localizes", "localize", "localise", "localises", "localized", "localised", "localizing", "localising", "causal circuit", "disease-specific", "specific network", "specificity confirmed")
CLAIM_TEMPLATE = "network associated with {}"
RESULTS_MAY_DIFFER = "results may differ slightly between runs"
#: Citations of methods used by some runs (added to the citation list only when used)
METHOD_CITATIONS = {
    "fedorov2012": "Fedorov A, Beichel R, Kalpathy-Cramer J, et al. 3D Slicer as an image computing platform for the Quantitative Imaging Network. Magn Reson Imaging. 2012;30(9):1323-1341. doi:10.1016/j.mri.2012.05.001",
    "winkler2014": "Winkler AM, Ridgway GR, Webster MA, Smith SM, Nichols TE. Permutation inference for the general linear model. NeuroImage. 2014;92:381-397. doi:10.1016/j.neuroimage.2014.01.060",
    "smith2009": "Smith SM, Nichols TE. Threshold-free cluster enhancement: addressing problems of smoothing, threshold dependence and localisation in cluster inference. NeuroImage. 2009;44(1):83-98. doi:10.1016/j.neuroimage.2008.03.061",
    "benjamini1995": "Benjamini Y, Hochberg Y. Controlling the false discovery rate: a practical and powerful approach to multiple testing. J R Stat Soc B. 1995;57(1):289-300. doi:10.1111/j.2517-6161.1995.tb02031.x",
    "brett2001": "Brett M, Leff AP, Rorden C, Ashburner J. Spatial normalization of brain images with focal lesions using cost function masking. NeuroImage. 2001;14(2):486-500. doi:10.1006/nimg.2001.0845",
}
SECTION_TITLES = {
    "pipeline": "Pipeline",
    "lesions": "Lesions",
    "seeds": "Seeds and map statistic",
    "connectome": "Connectome",
    "maps": "Network maps",
    "parameters": "Parameters",
    "lesion_qa": "Lesions and QA",
    "statistics": "Statistics",
    "thresholds": "Thresholds",
    "qa": "Connectome-confound QA",
    "deviations": "Deviations from the analysis plan",
    "limitations": "Limitations",
    "software": "Software",
    "citations": "Citations",
    "acknowledgements": "Acknowledgements",
    "disclaimer": "Disclaimer",
}
PIPELINE_JSON = "pipeline.json"
NORMALIZATION_JSON = "normalization.json"
RECORDS_TSV = ("sources", "records.tsv")
INTERRATER_TSV = ("derivatives", "qc", "interrater.tsv")
PLAN_JSON = ("derivatives", "lnm", "analysis_plan.json")
DEVIATIONS_JSONL = ("derivatives", "lnm", "analysis_plan_deviations.jsonl")
LESIONSET_JSON = ("derivatives", "lnm", "lesionset.json")
SOURCE_TYPE_LABELS = {"own_scan": "own scans", "case_report": "published case reports", "dataset": "shared datasets", "control_db": "control lesion databases"}
EXTRUSION_SEMANTICS = {
    "fractional": "fractional, exact total thickness of {total} mm (edge slices fractional)",
    "binary": "binary ±1 mm (3 voxels at 1 mm)",
    "single": "kept as a single slice",
}
# Registry kinds reported in the statistics section, in this order (other kinds follow in registry order)
STATISTICS_KINDS = ("cohort", "glm", "slnm", "nulls", "validation", "robustness", "vlsm", "circuits")
KIND_LABELS = {
    "cohort": "Overlap (sensitivity) maps",
    "glm": "General linear model",
    "slnm": "Symptom lesion network mapping",
    "nulls": "Null models",
    "validation": "Out-of-sample validation",
    "robustness": "Robustness",
    "vlsm": "Voxel-based lesion-symptom mapping",
    "circuits": "Circuits",
}
NULL_CAPTION = "Null models benchmark significance; they do not by themselves establish disease specificity (van den Heuvel et al. 2026b); see also Zalesky & Cash 2026; Siddiqi et al. 2026."
#: Inflections of "localize" (claim wording); "localization" stays allowed (the title of Boes et al., 2015)
_LOCALIZE_INFLECTIONS = r"locali[sz](?:e|es|ed|ing)"
_BANNED_PATTERN = re.compile(r"\b(?:" + "|".join([_LOCALIZE_INFLECTIONS, *(re.escape(phrase) for phrase in BANNED_PHRASES)]) + r")\b", re.IGNORECASE)


# Sections


@dataclasses.dataclass
class Section:
    """One methods section: an id, a title and blocks.

    Blocks are ("p", text), ("ul", [items]) or ("table", [header cells], [[row cells], ...]).
    """

    id: str
    title: str
    blocks: list = dataclasses.field(default_factory=list)

    def paragraph(self, text):
        if text:
            self.blocks.append(("p", str(text)))
        return self

    def bullets(self, items):
        items = [str(item) for item in items if item]
        if items:
            self.blocks.append(("ul", items))
        return self

    def table(self, header, rows):
        self.blocks.append(("table", [str(cell) for cell in header], [[_cell(cell) for cell in row] for row in rows]))
        return self

    def text(self):
        """All text of the section (for searching and the wording lint)."""
        parts = [self.title]
        for block in self.blocks:
            if block[0] == "p":
                parts.append(block[1])
            elif block[0] == "ul":
                parts.extend(block[1])
            else:
                parts.extend(block[1])
                parts.extend(cell for row in block[2] for cell in row)
        return "\n".join(parts)

    def to_dict(self):
        out = []
        for block in self.blocks:
            if block[0] == "table":
                out.append({"type": "table", "header": block[1], "rows": block[2]})
            else:
                out.append({"type": block[0], "content": block[1]})
        return {"id": self.id, "title": self.title, "blocks": out}


def _section(section_id, title=None):
    return Section(section_id, title or SECTION_TITLES.get(section_id, section_id.capitalize()))


def _cell(value):
    if value is None:
        return NOT_RECORDED
    return str(value)


def lint_wording(text):
    """Banned phrases found in ``text`` (lower case, in order of appearance; empty when the wording is clean)."""
    return [match.group(0).lower() for match in _BANNED_PATTERN.finditer(text or "")]


def find_section(sections, section_id):
    """The section with this id, or None."""
    return next((section for section in sections if section.id == section_id), None)


# Rendering


def render(sections, fmt="md", title="Methods", standalone=False):
    """Text of the sections: ``fmt`` "md" (Markdown), "html" (a fragment of <section> elements, or a
    complete UTF-8 page with ``standalone``) or "txt" (plain text).
    """
    if fmt == "md":
        return _render_md(sections, title)
    if fmt == "html":
        return _render_html(sections, title, standalone)
    if fmt == "txt":
        return _render_txt(sections, title)
    raise ValueError(f"Unknown methods format {fmt!r} (md, html or txt)")


def _md_cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def _render_md(sections, title):
    lines = [f"# {title}", ""] if title else []
    for section in sections:
        lines += [f"## {section.title}", ""]
        for block in section.blocks:
            if block[0] == "p":
                lines += [block[1], ""]
            elif block[0] == "ul":
                lines += [f"- {item}" for item in block[1]] + [""]
            else:
                lines.append("| " + " | ".join(_md_cell(cell) for cell in block[1]) + " |")
                lines.append("|" + "---|" * len(block[1]))
                lines += ["| " + " | ".join(_md_cell(cell) for cell in row) + " |" for row in block[2]]
                lines.append("")
    return "\n".join(lines)


def _render_html(sections, title, standalone):
    parts = []
    for section in sections:
        parts.append(f'<section id="methods-{html.escape(section.id)}">')
        parts.append(f"<h2>{html.escape(section.title)}</h2>")
        for block in section.blocks:
            if block[0] == "p":
                parts.append(f"<p>{html.escape(block[1])}</p>")
            elif block[0] == "ul":
                parts.append("<ul>" + "".join(f"<li>{html.escape(item)}</li>" for item in block[1]) + "</ul>")
            else:
                header = "".join(f"<th>{html.escape(cell)}</th>" for cell in block[1])
                rows = "".join("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in block[2])
                parts.append(f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>")
        parts.append("</section>")
    body = "\n".join(parts)
    if not standalone:
        return body + "\n"
    heading = f"<h1>{html.escape(title)}</h1>\n" if title else ""
    style = "body{font-family:sans-serif;max-width:60em;margin:1em auto;padding:0 1em;line-height:1.45}table{border-collapse:collapse}th,td{border:1px solid #999;padding:2px 6px;text-align:left;vertical-align:top}"
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{html.escape(title or 'Methods')}</title>\n<style>{style}</style>\n</head>\n<body>\n{heading}{body}\n</body>\n</html>\n"
    )


def _render_txt(sections, title):
    lines = [title, "=" * len(title), ""] if title else []
    for section in sections:
        lines += [section.title, "-" * len(section.title), ""]
        for block in section.blocks:
            if block[0] == "p":
                lines += [block[1], ""]
            elif block[0] == "ul":
                lines += [f"- {item}" for item in block[1]] + [""]
            else:
                lines.append(" | ".join(block[1]))
                lines += [" | ".join(row) for row in block[2]]
                lines.append("")
    return "\n".join(lines)


# Small formatting helpers


def _number(value, digits=3):
    """A number as short text (None, NaN and non-numbers: None)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value == int(value) and abs(value) < 1e15:
        return f"{int(value):,}" if abs(value) >= 10000 else str(int(value))
    return f"{value:.{digits}g}"


def _or_nr(value):
    return NOT_RECORDED if value is None or value == "" else str(value)


def _counts(values, labels=None, missing_label=NOT_RECORDED):
    """'a 3, b 2, not recorded 1' (in first-seen order; missing values counted under ``missing_label``)."""
    counts = {}
    for value in values:
        key = missing_label if value is None else (labels or {}).get(value, value)
        counts[key] = counts.get(key, 0) + 1
    return ", ".join(f"{key} {count}" for key, count in counts.items())


def _median(values):
    values = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.median(values) if values else None


def _missing(value):
    return value is None or (isinstance(value, str) and value.strip().lower() in ("", "n/a", "na", "nan"))


def _as_float(value):
    if _missing(value):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _short_sha(value):
    return value[:12] if isinstance(value, str) and len(value) == 64 else value


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_tsv(path):
    """Rows of a TSV file as dicts (None when the file is absent or unreadable)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line.rstrip("\r\n") for line in f if line.strip()]
    except (OSError, UnicodeDecodeError):
        return None
    if not lines:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"), strict=False)) for line in lines[1:]]


def _read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError):
        return None
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


# What was run: the study, the registry and the files the runs wrote


class Records:
    """The study, the run registry and the files of a root folder, read once for methods and the checklist.

    :param root: a study folder or any output root holding ``derivatives/lnm/runs.jsonl``.
    :param study: a ``study.Study`` (default: opened from ``root`` when it is a study).
    :param runs: a ``runs.load()`` result to reuse (default: loaded from ``root``).
    :param software: optional {"slicer": version, "fork_revision": revision} of the exporting application.
    :param include_dirs: extra folders holding run outputs (provenance.json, glm.json, ...), for example
      the output folder of a pipeline run that is not registered yet.
    """

    def __init__(self, root, study=None, runs=None, software=None, include_dirs=()):
        from . import runs as runslib

        self.root = os.path.abspath(os.fspath(root))
        if study is None:
            study = _open_study(self.root)
        self.study = study
        if runs is None:
            try:
                runs = runslib.load(self.root)
            except OSError:
                runs = {}
        self.runs = runs
        self.software = dict(software or {})
        self.include_dirs = [os.path.abspath(os.fspath(folder)) for folder in include_dirs if folder]
        self._cache = {}

    # Paths

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def relpath(self, path):
        try:
            rel = os.path.relpath(path, self.root)
        except ValueError:
            return path
        return path if rel.startswith("..") else rel.replace(os.sep, "/")

    def run_dir(self, record):
        from . import runs as runslib

        try:
            return runslib.run_dir(self.root, record)
        except (KeyError, TypeError, ValueError):
            return None

    # Rows

    @property
    def all_rows(self):
        return self.study.subjects if self.study is not None else []

    @property
    def rows(self):
        """Included rows (include = 1), in table order."""
        return [row for row in self.all_rows if row.get("include", "1") != "0"]

    # Registry

    def done_runs(self, kind):
        kinds = {kind} if isinstance(kind, str) else set(kind)
        return [record for record in self.runs.values() if record.get("kind") in kinds and record.get("status") == "done"]

    def summary(self, record):
        from . import runs as runslib

        try:
            value = runslib.read_summary(self.root, record)
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def params(self, record):
        from . import runs as runslib

        try:
            value = runslib.read_params(self.root, record)
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def run_json(self, record, name):
        folder = self.run_dir(record)
        return _read_json(os.path.join(folder, name)) if folder else None

    def _folders(self):
        """Folders that may hold run outputs: the registry's run folders, derivatives/lnm/*, the extra folders and the root."""
        if "folders" in self._cache:
            return self._cache["folders"]
        folders = []
        for record in self.runs.values():
            folder = self.run_dir(record)
            if folder:
                folders.append(folder)
        folders += sorted(glob.glob(os.path.join(self.root, "derivatives", "lnm", "*")))
        folders += self.include_dirs + [self.root]
        out, seen = [], set()
        for folder in folders:
            real = os.path.realpath(folder)
            if real not in seen and os.path.isdir(folder):
                seen.add(real)
                out.append(folder)
        self._cache["folders"] = out
        return out

    def group_records(self, name, kind):
        """[(run id or None, content)] of a group record (glm.json, slnm.json) of done runs and extra folders."""
        out, seen = [], set()
        for record in self.done_runs(kind):
            folder = self.run_dir(record)
            data = _read_json(os.path.join(folder, name)) if folder else None
            if data is not None:
                seen.add(os.path.realpath(folder))
                out.append((record["id"], data))
        for folder in self.include_dirs:
            data = _read_json(os.path.join(folder, name))
            if data is not None and os.path.realpath(folder) not in seen and data.get("status", "done") == "done":
                out.append((data.get("run_id"), data))
        return out

    def map_records(self):
        """Network-mapping runs as uniform dicts: {"id", "source", "bundle", "n", "subject_ids_sha256",
        "subject_list", "supersample", "probabilistic", "statistic", "null", "qa" {lesion: qa values},
        "threshold", "reference", "split_half", "versions", "software"}.
        """
        if "maps" in self._cache:
            return self._cache["maps"]
        out, seen = [], set()
        for record in self.done_runs("cohortmaps"):
            folder = self.run_dir(record)
            data = _read_json(os.path.join(folder, "cohortmaps.json")) if folder else None
            if data is None:
                continue
            seen.add(os.path.realpath(folder))
            subjects = data.get("subjects") or {}
            qa_values = data.get("qa") or {}
            reference = next(((values or {}).get("reference") for values in qa_values.values() if (values or {}).get("reference")), None)
            out.append({
                "id": record["id"],
                "source": "cohortmaps",
                "bundle": data.get("bundle") or {},
                "n": data.get("n"),
                "subject_ids_sha256": data.get("subject_ids_sha256"),
                "subject_list": {"name": subjects.get("list_name"), "sha256": subjects.get("list_sha256")} if isinstance(subjects, dict) else {},
                "supersample": data.get("supersample"),
                "probabilistic": data.get("probabilistic"),
                "statistic": data.get("statistic"),
                "null": data.get("null"),
                "qa": qa_values,
                "threshold": None,
                "reference": {"definition": "lnm-reference-v1"} if reference else None,
                "split_half": None,
                "versions": data.get("versions") or record.get("software"),
                "software": None,
            })
        run_ids = {os.path.realpath(self.run_dir(record) or ""): record["id"] for record in self.runs.values() if self.run_dir(record)}
        for folder in self._folders():
            real = os.path.realpath(folder)
            if real in seen:
                continue
            provenance = _read_json(os.path.join(folder, "provenance.json"))
            if provenance is None or provenance.get("format") != export.PROVENANCE_FORMAT:
                continue
            seen.add(real)
            out.append(_map_record_from_provenance(provenance, folder, run_ids.get(real)))
        self._cache["maps"] = out
        return out

    def connectomes(self):
        """Distinct connectomes of the map records (by manifest sha256, else name), with the records using each."""
        groups = {}
        for record in self.map_records():
            bundle = record["bundle"] or {}
            key = bundle.get("manifest_sha256") or bundle.get("name") or NOT_RECORDED
            groups.setdefault(key, {"bundle": bundle, "records": []})["records"].append(record)
        return list(groups.values())

    def pipeline(self):
        """(pipeline.json content, run id or None), or (None, None)."""
        for folder in [*self.include_dirs, self.root]:
            data = _read_json(os.path.join(folder, PIPELINE_JSON))
            if data is not None:
                return data, data.get("run_id")
        records = self.done_runs("pipeline") or [record for record in self.runs.values() if record.get("kind") == "pipeline"]
        for record in reversed(records):
            data = self.run_json(record, PIPELINE_JSON)
            if data is not None:
                return data, record["id"]
        return None, None

    def normalizations(self):
        """normalization.json contents of the study's native-space lesions (derivatives/normalization/*/ and normalization runs)."""
        if "normalizations" in self._cache:
            return self._cache["normalizations"]
        paths = sorted(glob.glob(os.path.join(self.root, "derivatives", "normalization", "*", NORMALIZATION_JSON)))
        for record in self.done_runs("normalization"):
            folder = self.run_dir(record)
            if folder:
                paths.append(os.path.join(folder, NORMALIZATION_JSON))
        out, seen = [], set()
        for path in paths:
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            data = _read_json(path)
            if data is not None and data.get("status", "done") == "done":
                out.append(data)
        self._cache["normalizations"] = out
        return out

    def figure_fits(self):
        """Figure landmark fits (sources/<record>/fig-<k>_fit.json): [{"rms_mm", "loo_rms_mm"}]."""
        out = []
        for path in sorted(glob.glob(os.path.join(self.root, "sources", "*", "fig-*_fit.json"))):
            data = _read_json(path)
            if data is None:
                continue
            rms = _first_number(data, ("rms_mm", "rms", "residual_rms_mm"))
            loo = _first_number(data, ("loo_rms_mm", "leave_one_out_rms_mm", "loo_rms"))
            out.append({"rms_mm": rms, "loo_rms_mm": loo})
        return out

    def lesionset(self):
        return _read_json(self.path(*LESIONSET_JSON))

    def records_tsv(self):
        return _read_tsv(self.path(*RECORDS_TSV))

    def interrater(self):
        return _read_tsv(self.path(*INTERRATER_TSV))

    def deviations(self):
        """(analysis plan exists, deviation records or None when the log is absent)."""
        return os.path.isfile(self.path(*PLAN_JSON)), _read_jsonl(self.path(*DEVIATIONS_JSONL))


def _open_study(root):
    from .study import Study

    try:
        return Study.open(root) if Study.is_study(root) else None
    except (OSError, ValueError):
        return None


def _first_number(data, keys):
    for key in keys:
        value = _as_float(data.get(key))
        if value is not None:
            return value
    return None


def _map_record_from_provenance(provenance, folder, run_id):
    key = export.compatibility_key(provenance)
    qa_values = {lesion.get("name"): lesion.get("qa") for lesion in provenance.get("lesions") or [] if isinstance(lesion, dict)}
    qa_json = _read_json(os.path.join(folder, "qa.json")) or {}
    reference = qa_json.get("reference")
    if reference is None and any((values or {}).get("reference") for values in qa_values.values()):
        reference = {"definition": "lnm-reference-v1"}
    return {
        "id": run_id or os.path.basename(folder),
        "source": "provenance",
        "bundle": provenance.get("connectome") or {},
        "n": provenance.get("N"),
        "subject_ids_sha256": provenance.get("subject_ids_sha256"),
        "subject_list": provenance.get("subject_list") or {},
        "supersample": key[3],
        "probabilistic": key[4],
        "statistic": key[2],
        "null": provenance.get("null"),
        "qa": qa_values,
        "threshold": provenance.get("threshold"),
        "reference": reference,
        "split_half": qa_json.get("split_half"),
        "versions": provenance.get("versions"),
        "software": provenance.get("software"),
    }


# Section builders


def methods_sections(root, study=None, runs=None, software=None, include_dirs=(), records=None):
    """Ordered :class:`Section` list of a study's methods, from its registry and files.

    :param root: study folder (or an output root with ``derivatives/lnm/runs.jsonl``).
    :param study: ``study.Study`` (default: opened from ``root``).
    :param runs: a ``runs.load()`` result to reuse.
    :param software: optional {"slicer": version, "fork_revision": revision}.
    :param include_dirs: extra output folders (see :class:`Records`).
    :param records: a :class:`Records` to reuse (the other arguments are then ignored).
    """
    records = records or Records(root, study=study, runs=runs, software=software, include_dirs=include_dirs)
    used = set()
    sections = [
        pipeline_section(records),
        lesions_section(records, used),
        seeds_section(records),
        connectome_section(records),
        statistics_section(records, used),
        thresholds_section(records),
        qa_section(records),
        deviations_section(records),
        limitations_section(records),
        software_section(records),
    ]
    bundles = [group["bundle"] for group in records.connectomes()]
    sections.append(citations_section(bundles, used))
    acknowledgements = acknowledgements_section(bundles)
    if acknowledgements is not None:
        sections.append(acknowledgements)
    sections.append(disclaimer_section())
    return sections


def pipeline_section(records):
    section = _section("pipeline")
    data, run_id = records.pipeline()
    if data is None:
        return section.paragraph(f"Pipeline: {NOT_RECORDED} (no {PIPELINE_JSON}).")
    head = f"The analysis ran as one pipeline (run {run_id or NOT_RECORDED}"
    recipe = data.get("recipe")
    outside = []
    if isinstance(recipe, dict):
        outside = [str(step) for step in recipe.get("outside_pipeline") or []]
        recipe = recipe.get("name") or recipe.get("id")
    head += f"; recipe {recipe})." if recipe else ")."
    if outside:
        head += f" Recipe steps outside the pipeline (not part of this run): {', '.join(outside)}."
    section.paragraph(head)
    stages = data.get("stages")
    if isinstance(stages, dict):
        stages = [{"name": name, **(value if isinstance(value, dict) else {"status": value})} for name, value in stages.items()]
    items = []
    for stage in stages or []:
        if not isinstance(stage, dict):
            continue
        provider = stage.get("provider")
        if isinstance(provider, dict):
            provider = provider.get("name") or provider.get("module")
        status = stage.get("status") or NOT_RECORDED
        text = f"{stage.get('name') or stage.get('id') or NOT_RECORDED}: provider {_or_nr(provider)}; {status}"
        reason = stage.get("reason") or stage.get("skip_reason") or stage.get("reuse_reason")
        if reason:
            text += f" ({reason})"
        items.append(text)
    if items:
        section.bullets(items)
    else:
        section.paragraph(f"Stages: {NOT_RECORDED}.")
    overrides = data.get("overrides")
    if overrides:
        section.bullets(f"Override: {_describe_item(item)}" for item in (overrides if isinstance(overrides, list) else [overrides]))
    else:
        section.paragraph("Overrides: none recorded." if "overrides" in data else f"Overrides: {NOT_RECORDED}.")
    exclusions = data.get("exclusions") if "exclusions" in data else data.get("excluded")
    if isinstance(exclusions, dict) and exclusions:
        section.bullets(f"Excluded ({reason}): {len(ids) if isinstance(ids, list) else ids} lesion(s)" for reason, ids in exclusions.items())
    elif isinstance(exclusions, list) and exclusions:
        section.bullets(f"Excluded: {_describe_item(item)}" for item in exclusions)
    else:
        section.paragraph("Exclusions: none recorded." if exclusions is not None else f"Exclusions: {NOT_RECORDED}.")
    return section


def _describe_item(item):
    if isinstance(item, dict):
        return "; ".join(f"{key} {value}" for key, value in item.items() if value not in (None, ""))
    return str(item)


def lesions_section(records, used):
    """Lesion intake: sources, PRISMA counts, template, figure fits, conventions, dimensionality and
    extrusion, mirroring, raters, review, inter-rater agreement, normalization and assisted tracing.
    """
    section = _section("lesions")
    study = records.study
    if study is None:
        return section.paragraph(f"Lesion intake: {NOT_RECORDED} (no study).")
    rows, all_rows = records.rows, records.all_rows
    section.paragraph(f"{len(rows)} lesion(s) were included ({len(all_rows)} row(s) in participants.tsv; {len(all_rows) - len(rows)} with include = 0).")
    items = []

    def column(name):
        return [None if _missing(row.get(name)) else row.get(name) for row in rows]

    items.append(f"Sources: {_counts(column('source_type'), SOURCE_TYPE_LABELS) or NOT_RECORDED}")
    records_rows = records.records_tsv()
    if records_rows is None:
        items.append(f"PRISMA counts: {NOT_RECORDED} (no sources/records.tsv)")
    else:
        stage_column = next((name for name in ("prisma_stage", "stage", "decision", "status", "screening") if records_rows and name in records_rows[0]), None)
        text = f"PRISMA: {len(records_rows)} record(s) in sources/records.tsv"
        if stage_column:
            text += f"; {stage_column}: " + _counts(row.get(stage_column) or None for row in records_rows)
        items.append(text)
    policy = study.trace_policy
    template = study.template or {}
    template_name = template.get("id") or template.get("name") or policy.get("space") or study.space
    stored = study.metadata.get("trace_policy") if isinstance(getattr(study, "metadata", None), dict) else None
    # The trace_policy property fills in res = 1 by default; only a recorded resolution is reported
    resolution = (stored.get("res") if isinstance(stored, dict) else None) or template.get("res")
    items.append(f"Tracing template: {_or_nr(template_name)}" + (f" at {_number(resolution)} mm" if _number(resolution) else f"; resolution {NOT_RECORDED}"))
    trace_templates = column("trace_template")
    if any(trace_templates):
        items.append(f"Templates traced on (per lesion): {_counts(trace_templates)}")
    fits = records.figure_fits()
    figure_rows = [row for row in rows if "figure" in (row.get("trace_method") or "").split("+") or not _missing(row.get("figure"))]
    rms = _median(fit["rms_mm"] for fit in fits) if fits else _median(_as_float(row.get("fit_rms_mm")) for row in rows)
    loo = _median(fit["loo_rms_mm"] for fit in fits)
    if fits or figure_rows:
        items.append(f"Figure landmark fits: median RMS {_number(rms) + ' mm' if _number(rms) else NOT_RECORDED}, median leave-one-out RMS {_number(loo) + ' mm' if _number(loo) else NOT_RECORDED} ({len(fits)} fit file(s); the residual measures landmark consistency, not lesion accuracy)")
    else:
        items.append("Figure landmark fits: none (no figure tracings)")
    if figure_rows or any(column("fig_convention")):
        items.append(f"Figure left-right convention: {_counts([None if _missing(row.get('fig_convention')) else row['fig_convention'] for row in figure_rows or rows])}")
    items.append(f"Dimensionality: {_counts(column('dimensionality'))}")
    extrude = policy.get("extrude_2d")
    items.append(f"2D extrusion policy: {_extrusion_text(extrude)}")
    flipped = [value for value in column("flipped") if value is not None]
    mirrored = sum(1 for value in flipped if str(value) == "1")
    items.append(f"Mirrored left-right: {mirrored} lesion(s) (approximate: templates are asymmetric)" if flipped else f"Mirrored left-right: {NOT_RECORDED}")
    tracers = [row.get("tracer") if not _missing(row.get("tracer")) else row.get("rater") for row in rows]
    tracers = [None if _missing(value) else value for value in tracers]
    known = sorted({value for value in tracers if value})
    items.append(f"Raters: {len(known)} ({', '.join(known)}); {sum(1 for value in tracers if value is None)} lesion(s) without a recorded rater" if known else f"Raters: {NOT_RECORDED}")
    reviewers = sorted({row["reviewer"] for row in rows if not _missing(row.get("reviewer"))})
    review = _counts(column("review_status"))
    items.append(f"Review: required {'yes' if study.review_required else 'no'}; states {review}; reviewers {len(reviewers) if reviewers else NOT_RECORDED}")
    interrater = records.interrater()
    if interrater is None:
        items.append(f"Inter-rater agreement: {NOT_RECORDED} (no derivatives/qc/interrater.tsv)")
    else:
        dice = _median(_as_float(row.get("dice")) for row in interrater)
        items.append(f"Inter-rater agreement: {len(interrater)} pair(s); median Dice {_number(dice) or NOT_RECORDED}")
    normalized = [row for row in rows if not _missing(row.get("norm_method")) or not _missing(row.get("lesion_native")) or row.get("space") == "native"]
    if normalized:
        passed = sum(1 for row in normalized if row.get("norm_qc") == "pass")
        items.append(
            f"Normalization of native-space lesions: {len(normalized)} lesion(s); methods {_counts([None if _missing(row.get('norm_method')) else row['norm_method'] for row in normalized])}; "
            f"QC pass {passed} of {len(normalized)} ({100.0 * passed / len(normalized):.0f}%), states {_counts([None if _missing(row.get('norm_qc')) else row['norm_qc'] for row in normalized])}",
        )
    else:
        items.append("Normalization: none (no native-space lesions)")
    methods = [set((row.get("trace_method") or "").split("+")) if not _missing(row.get("trace_method")) else None for row in rows]
    items.append(f"Tracing methods recorded for {sum(1 for value in methods if value)} of {len(rows)} lesion(s)")
    assisted = [row for row, tokens in zip(rows, methods, strict=True) if tokens and "smartfill" in tokens]
    if assisted:
        median_pct = _median(_as_float(row.get("assist_pct")) for row in assisted)
        items.append(f"assisted tracing ({len(assisted)} lesions, median {_number(median_pct, 2) + '%' if _number(median_pct) is not None else NOT_RECORDED + ' %'} of voxels)")
    automatic = [row for row, tokens in zip(rows, methods, strict=True) if tokens and any(token.startswith("auto:") for token in tokens)]
    if automatic:
        approved = sum(1 for row in automatic if row.get("review_status") == "approved")
        items.append(f"Automatic segmentation: {len(automatic)} lesion(s), {approved} approved by a human reviewer")
    section.bullets(items)
    normalizations = records.normalizations()
    if normalizations:
        used.add("brett2001")
        section.paragraph(normalization_paragraph(normalizations, rows))
    return section


def _extrusion_text(extrude):
    if not extrude:
        return NOT_RECORDED
    if isinstance(extrude, str):
        extrude = {"mode": extrude}
    if not isinstance(extrude, dict):
        return str(extrude)
    if extrude.get("semantics"):
        return str(extrude["semantics"])
    mode = str(extrude.get("mode") or "")
    key = "fractional" if mode.startswith("frac") else "binary" if mode.startswith("bin") else "single" if mode.startswith(("single", "keep", "none")) else None
    if key is None:
        return f"{mode or NOT_RECORDED}"
    return EXTRUSION_SEMANTICS[key].format(total=_number(extrude.get("total_mm")) or "2")


def normalization_paragraph(normalizations, rows=()):
    """Lesion-intake paragraph of normalization.json records (BRAINSFit, cost-function masking, quality,
    template, attempts, two-step, review counts).
    """
    methods = [record.get("method") or {} for record in normalizations]
    versions = sorted({str(method.get("version")) for method in methods if method.get("version")})
    dilations = sorted({_number(method.get("dilate_mm")) for method in methods if _number(method.get("dilate_mm")) is not None})
    qualities = _counts([method.get("quality") for method in methods])
    grids = sorted({"x".join(str(v) for v in method["grid"]) for method in methods if isinstance(method.get("grid"), (list, tuple))})
    templates = _counts([(record.get("template") or {}).get("id") for record in normalizations])
    attempts = [len(method["attempts"]) for method in methods if isinstance(method.get("attempts"), list)]
    two_step = sum(1 for method in methods if method.get("two_step"))
    suggestions = _counts([(record.get("qc") or {}).get("suggestion") for record in normalizations])
    verdicts = _counts([(record.get("review") or {}).get("verdict") for record in normalizations], missing_label="not reviewed")
    masking = f"the lesion dilated by {', '.join(dilations)} mm excluded from the similarity metric (cost-function masking; Brett et al., 2001)" if dilations else f"cost-function masking margin {NOT_RECORDED}"
    if any(value == "-1" for value in dilations):
        masking += "; masking disabled for some lesions"
    text = (
        f"{len(normalizations)} native-space lesion(s) were normalized to the template with BRAINSFit {', '.join(versions) if versions else NOT_RECORDED} "
        f"(affine, then BSpline unless the quality was \"affine\"), with {masking}. "
        f"Quality: {qualities}{' (BSpline grid ' + ', '.join(grids) + ')' if grids else ''}; templates: {templates}; "
        f"registration attempts per lesion: {'median ' + _number(statistics.median(attempts)) + ', max ' + str(max(attempts)) if attempts else NOT_RECORDED}; "
        f"two-step registration through a T1w scan: {two_step}. "
        f"Automatic QC suggestions: {suggestions}; reviewer verdicts: {verdicts}"
    )
    norm_qc = [row.get("norm_qc") for row in rows if not _missing(row.get("norm_qc"))]
    if norm_qc:
        text += f"; norm_qc in participants.tsv: {_counts(norm_qc)}"
    return text + f". BRAINSFit samples randomly and multi-threaded, so {RESULTS_MAY_DIFFER}; reruns with the same inputs reuse the stored result."


def seeds_section(records):
    section = _section("seeds")
    maps = records.map_records()
    if not maps:
        return section.paragraph(f"Seeds: {NOT_RECORDED} (no network-mapping run).")
    items = []
    for record in maps:
        probabilistic = record.get("probabilistic")
        seed = "fractional voxel occupancy (probabilistic lesions)" if probabilistic else "fractional voxel occupancy of the binarized lesion" if probabilistic is False else f"seed weighting {NOT_RECORDED}"
        supersample = record.get("supersample")
        items.append(
            f"Run {record['id']}: each lesion was resampled onto the connectome grid as {seed} (supersampling {_or_nr(supersample)} per axis); "
            f"the occupancy-weighted mean time course of the lesion was correlated with every voxel (Pearson r), averaged over runs and Fisher z-transformed; "
            f"stored maps: mean r, mean Fisher z and the voxelwise one-sample t of Fisher z (statistic recorded for pooling: {_or_nr(record.get('statistic'))})",
        )
    section.bullets(items)
    inputs = sorted({str((data.get("inputs") or {}).get("input") or data.get("input")) for _, data in records.group_records("glm.json", "glm") + records.group_records("slnm.json", "slnm") if (data.get("inputs") or {}).get("input") or data.get("input")})
    if inputs:
        section.paragraph(f"Group statistics used the {', '.join(inputs)} maps as input (Fz is independent of the connectome N).")
    return section


def connectome_section(records):
    section = _section("connectome")
    groups = records.connectomes()
    if not groups:
        return section.paragraph(f"Connectome: {NOT_RECORDED} (no network-mapping run).")
    rows = []
    for group in groups:
        bundle = group["bundle"]
        first = group["records"][0]
        subject_list = first.get("subject_list") or {}
        gsr = bundle.get("gsr")
        preprocessing = bundle.get("preprocessing") or {}
        rows.append([
            bundle.get("name") or NOT_RECORDED,
            f"doi:{bundle['doi']}" if bundle.get("doi") else NOT_RECORDED,
            _or_nr(bundle.get("dataset_version")),
            f"{_or_nr(first.get('n'))} of {_or_nr(bundle.get('n_subjects'))}",
            _or_nr(subject_list.get("sha256") or first.get("subject_ids_sha256")),
            _or_nr(subject_list.get("name") or bundle.get("subset")) if subject_list.get("name") or bundle.get("subset") else "all subjects",
            "yes" if gsr is True else "no" if gsr is False else NOT_RECORDED,
            _or_nr(preprocessing.get("pipelineUrl") or preprocessing.get("url")),
            _or_nr(bundle.get("mask_sha256")),
            _or_nr(bundle.get("licence")),
        ])
    section.table(["Connectome", "DOI", "Version", "N used", "Subject list sha256", "Subset", "GSR", "Preprocessing", "Mask sha256", "Data use terms"], rows)
    return section


def statistics_section(records, used):
    section = _section("statistics")
    found = False
    for _, data in records.group_records("cohort.json", "cohort"):
        found = True
        section.paragraph(f"{KIND_LABELS['cohort']}: {_generic_text(data, ('n_maps', 'rule', 'overlap', 'conjunction'))}. The classic overlap map is hub-inflated and is reported next to a null-referenced version when nulls were run.")
    for run_id, data in records.group_records("glm.json", "glm"):
        found = True
        used.update(("winkler2014", "benjamini1995"))
        if (data.get("tfce") or {}).get("enabled"):
            used.add("smith2009")
        section.paragraph(glm_paragraph(data, run_id))
    for run_id, data in records.group_records("slnm.json", "slnm"):
        found = True
        used.update(("winkler2014", "benjamini1995"))
        section.paragraph(slnm_paragraph(data, run_id))
    for kind in [*STATISTICS_KINDS[3:], *sorted({r.get("kind") for r in records.runs.values()} - set(STATISTICS_KINDS) - {"map", "cohortmaps", "reference", "preflight", "figure", "report", "export", "normalization", "pipeline", "fastindex", "calibration", "parity", None})]:
        for record in records.done_runs(kind):
            found = True
            summary = records.summary(record) or {}
            text = f"{KIND_LABELS.get(kind, kind.capitalize())} (run {record['id']}): {_generic_text(summary) or 'summary ' + NOT_RECORDED}."
            if kind == "nulls":
                text += f" {NULL_CAPTION}"
            section.paragraph(text)
    if not found:
        section.paragraph(f"Group statistics: {NOT_RUN} (no group analysis in the run registry).")
    section.paragraph(f"Results are described as the {CLAIM_TEMPLATE.format('the tested contrast or score')}; no test gives an automatic verdict on specificity.")
    return section


def _generic_text(data, keys=None):
    """'key value; ...' of the scalar values of a summary (the given keys first)."""
    if not isinstance(data, dict):
        return ""
    keys = [key for key in (keys or ()) if key in data] + [key for key in data if key not in (keys or ())]
    parts = []
    for key in keys:
        value = data.get(key)
        if isinstance(value, bool):
            parts.append(f"{key} {'yes' if value else 'no'}")
        elif isinstance(value, (int, float)):
            parts.append(f"{key} {_number(value) or NOT_RECORDED}")
        elif isinstance(value, str) and len(value) <= 80:
            parts.append(f"{key} {value}")
        if len(parts) >= 8:
            break
    return "; ".join(parts)


def _tails_text(tails):
    return "two-sided" if tails in (2, "2", "two", "two-sided") else "one-sided" if tails in (1, "1", "one", "one-sided") else f"tails {NOT_RECORDED}"


def _permutation_text(permutation):
    permutation = permutation or {}
    text = f"{permutation.get('method') or 'permutation'}; exchange {_or_nr(permutation.get('scheme'))}"
    if permutation.get("reason"):
        text += f" ({permutation['reason']})"
    if permutation.get("block_column") and permutation.get("scheme") not in (None, "free"):
        text += f", blocks from the {permutation['block_column']} column"
    text += f"; {_or_nr(permutation.get('n_perm'))} permutations"
    if permutation.get("exhaustive"):
        text += " (exhaustive enumeration)"
    if permutation.get("preview"):
        text += " (preview: fewer than 1000)"
    text += f", seed {_or_nr(permutation.get('seed'))}"
    return text


def glm_paragraph(data, run_id=None):
    """Methods text of a glm.json record."""
    contrast = data.get("contrast") or {}
    covariates = data.get("covariates")
    covariate_text = ", ".join(covariates) if covariates else "none"
    if data.get("volume_off_reason"):
        covariate_text += f" (lesion volume off: {data['volume_off_reason']})"
    effect = data.get("effect_size") or {}
    fwe, fdr = data.get("fwe") or {}, data.get("fdr") or {}
    tfce = data.get("tfce") or {}
    tfce_text = (
        f"TFCE (H = {_number(tfce.get('H'))}, E = {_number(tfce.get('E'))}, {_or_nr(tfce.get('steps'))} steps, {_or_nr(tfce.get('connectivity'))}-connectivity) with FWE from the maximum TFCE of each permutation (Smith & Nichols, 2009)"
        if tfce.get("enabled")
        else "TFCE was not used"
    )
    min_p = data.get("min_attainable_p", data.get("min_p"))
    return (
        f"{KIND_LABELS['glm']} (run {run_id or data.get('run_id') or NOT_RECORDED}): design {_or_nr(data.get('design_type'))} (test {_or_nr(data.get('test'))}), "
        f"contrast {_or_nr(contrast.get('label'))}, n = {_or_nr(data.get('n'))} ({_or_nr(data.get('n_excluded', 0))} excluded listwise); covariates: {covariate_text}. "
        f"Inference: {_permutation_text(data.get('permutation'))} (Winkler et al., 2014); statistic {_or_nr(data.get('stat'))}, {_tails_text(data.get('tails'))}, df = {_or_nr(data.get('df'))}. "
        f"Correction: voxelwise max-statistic FWE (primary, alpha {_number(fwe.get('alpha')) or NOT_RECORDED}) and Benjamini-Hochberg FDR (secondary, q {_number(fdr.get('alpha')) or NOT_RECORDED}); {tfce_text}; "
        "no cluster-extent inference (cluster tables are descriptive). "
        f"Effect size: {effect.get('name') or NOT_RECORDED} ({effect.get('formula') or NOT_RECORDED}). "
        f"The smallest attainable p is {_number(min_p) or NOT_RECORDED}; BH-FDR reaches q ≤ {_number(fdr.get('alpha')) or 0.05} only when at least {_or_nr(fdr.get('floor_voxels'))} voxels reach it (FDR floor)."
    )


def slnm_paragraph(data, run_id=None):
    """Methods text of an slnm.json record."""
    covariates = data.get("covariates")
    covariate_text = ", ".join(covariates) if covariates else "none"
    if data.get("volume_off_reason"):
        covariate_text += f" (lesion volume off: {data['volume_off_reason']})"
    inference = data.get("inference") or {}
    text = (
        f"{KIND_LABELS['slnm']} (run {run_id or data.get('run_id') or NOT_RECORDED}): score {_or_nr(data.get('score'))}, "
        f"{'Spearman (ranks of scores and maps)' if data.get('spearman') else 'Pearson'} partial r = t / sqrt(t² + df) from the GLM with the score as a continuous regressor, "
        f"n = {_or_nr(data.get('n'))}; covariates: {covariate_text}"
    )
    if data.get("flip"):
        text += "; the score was multiplied by -1 (sign flip)"
    text += f". Inference: {_permutation_text(data.get('permutation'))}; {_tails_text(inference.get('tails'))}; max-statistic FWE primary, BH-FDR secondary; the parametric p is not used."
    lodo = data.get("lodo")
    if isinstance(lodo, dict):
        datasets = lodo.get("datasets") or []
        if lodo.get("computed"):
            text += f" Leave-one-dataset-out over {len(datasets)} datasets: (n - 3)-weighted Fisher-z combination, mean off-diagonal spatial r {_number(lodo.get('mean_offdiag_r')) or NOT_RECORDED} (block-permutation p {_number(lodo.get('p_perm')) or NOT_RECORDED})."
        else:
            text += f" Leave-one-dataset-out: not computed ({'; '.join(lodo.get('warnings') or []) or NOT_RECORDED})."
    controls = data.get("controls") or []
    if controls:
        text += f" Control-score maps: {len(controls)}, compared with partial spatial r given [μC_r, PC1]."
    return text


def thresholds_section(records):
    from . import thresholds

    section = _section("thresholds")
    lines = []
    for record in records.map_records():
        threshold = record.get("threshold") or {}
        resolved = threshold.get("resolved")
        if not resolved:
            continue
        try:
            text = thresholds.describe(thresholds.Resolved.from_dict(resolved))
        except (ValueError, TypeError, KeyError):
            text = threshold.get("derived") or NOT_RECORDED
        p = resolved.get("p_unc")
        rule = (threshold.get("rule") or {}).get("kind") or NOT_RECORDED
        line = f"Run {record['id']}: rule {rule} {text}"
        if p is not None:
            line += f" (two-sided uncorrected p; Bonferroni FWE bound {thresholds.format_p(resolved.get('p_fwe'))} over V voxels)"
        elif resolved.get("statistic") == "Fz" or rule == "top_pct":
            line += " (no p equivalent: effect-size or rank rule)"
        if resolved.get("allowed") is False:
            line += " (applied with an explicit override)"
        if line not in lines:
            lines.append(line)
    if not lines:
        return section.paragraph(f"Threshold rules: {NOT_RECORDED}. Maps are stored unthresholded.")
    section.bullets(lines)
    return section.paragraph("Maps are stored unthresholded; thresholds are display and reporting rules re-derived from (rule, N, V). Fz and top-x% rules are alternatives to T rules, never equivalents.")


def qa_section(records):
    from . import qa as qalib

    section = _section("qa")
    maps = records.map_records()
    references = [record for record in maps if record.get("reference")]
    if not references:
        return section.paragraph(f"Connectome-confound QA (μC_r, PC1-3, hubness): {NOT_RECORDED}.")
    definitions = sorted({str(record["reference"].get("definition") or NOT_RECORDED) for record in references})
    section.paragraph(
        f"Reference maps: {', '.join(definitions)} (μC_r the exact mean r profile, PC1-3 the leading components of the column-centred group r matrix, hubness the mean |Fz| to sampled seeds). "
        "For each map, Pearson r and Spearman rho with μC_r, PC1-3 and hubness and the spatial R² on these four references were computed. "
        f"Chips are calibrated: the percentile of r(map, μC_r) among the maps of the lesion's relocated copies on the same voxel sample (below {qalib.CALIBRATED_LOW:g}th: "
        f"\"{qalib.CALIBRATED_TEXTS['below'].format(nulls=qalib.RELOCATED_NULLS)}\"; {qalib.CALIBRATED_LOW:g}th-{qalib.CALIBRATED_HIGH:g}th: \"{qalib.CALIBRATED_TEXTS['amber'].format(nulls=qalib.RELOCATED_NULLS)}\"; "
        f"above {qalib.CALIBRATED_HIGH:g}th: \"{qalib.CALIBRATED_TEXTS['red'].format(nulls=qalib.RELOCATED_NULLS)}\").",
    )
    rows = []
    for record in references:
        values = [(values or {}).get("reference") or {} for values in (record.get("qa") or {}).values()]
        percentiles = [value.get("percentile") for value in values if value.get("percentile") is not None]
        ks = sorted({value.get("k") for value in values if value.get("k")})
        levels = _counts([value.get("level") for value in values]) if values else NOT_RECORDED
        rows.append([
            record["id"],
            str(len(values)),
            _number(_median(value.get("mu_r") for value in values if value.get("mu_r") is not None)) or NOT_RECORDED,
            _number(_median(value.get("pc1_r") for value in values if value.get("pc1_r") is not None)) or NOT_RECORDED,
            _number(_median(percentiles)) or NOT_RECORDED,
            ", ".join(f"{100.0 / k:.2g}% at K={k}" for k in ks) if ks else NOT_RECORDED,
            levels,
        ])
    section.table(["Run", "Maps", "Median r(μC_r)", "Median r(PC1)", "Median percentile", "Resolution", "Chips"], rows)
    halves = [record.get("split_half") for record in maps if record.get("split_half")]
    if halves:
        section.paragraph("Connectome split-half (odd/even subjects): connectome sampling stability (≈0.99 expected for ≥ 500 per half; not evidence of robustness).")
    return section


def deviations_section(records):
    section = _section("deviations")
    plan, deviations = records.deviations()
    if deviations:
        items = []
        for item in deviations:
            what = item.get("description") or item.get("deviation") or item.get("what") or item.get("change") or _describe_item({k: v for k, v in item.items() if k not in ("reason", "time", "at", "created")}) or f"Deviation: {NOT_RECORDED}"
            reason = item.get("reason") or NOT_RECORDED
            when = item.get("time") or item.get("at") or item.get("created")
            items.append(f"{what} (reason: {reason}{'; ' + str(when) if when else ''})")
        return section.bullets(items)
    if plan:
        return section.paragraph("No deviations from the locked analysis plan were recorded.")
    return section.paragraph(f"Analysis plan and deviations: {NOT_RECORDED} (no locked analysis plan).")


def limitations_section(records):
    section = _section("limitations")
    groups = records.group_records("glm.json", "glm") + records.group_records("slnm.json", "slnm")
    items = []
    for _, data in groups:
        for item in data.get("limitations") or []:
            if item not in items:
                items.append(item)
    if items:
        return section.bullets(items)
    if groups:
        return section.paragraph("No design limitations were recorded by the group analyses.")
    return section.paragraph(f"Limitations: {NOT_RECORDED} (no group analysis).")


def software_text(software=None, versions=None):
    """'3D Slicer <v> (RRID:SCR_005619), Slicer Studio fork revision <r>, LesionNetworkMappingLib <v>, Python <v>, numpy <v>, scipy <v>'."""
    software = software or {}
    versions = versions or {}
    parts = [
        f"3D Slicer {software.get('slicer') or NOT_RECORDED} ({SLICER_RRID}; Fedorov et al., 2012)",
        f"Slicer Studio fork revision {software.get('fork_revision') or NOT_RECORDED}",
        f"{export.LIBRARY_NAME} {software.get(export.LIBRARY_NAME) or versions.get(export.LIBRARY_NAME) or export.LIBRARY_VERSION}",
    ]
    for name in ("python", "numpy", "scipy"):
        parts.append(f"{'Python' if name == 'python' else name} {versions.get(name) or NOT_RECORDED}")
    if versions.get("blas"):
        parts.append(f"BLAS {versions['blas']}")
    return ", ".join(parts)


def software_section(records):
    section = _section("software")
    software = {}
    seen = {}  # name: versions in order of appearance (failed and cancelled runs are left out)
    for record in records.map_records():
        software.update({key: value for key, value in (record.get("software") or {}).items() if value})
        for key, value in (record.get("versions") or {}).items():
            if value:
                seen.setdefault(key, {})[str(value)] = None
    for record in records.runs.values():
        if record.get("status") != "done":
            continue
        for key, value in (record.get("software") or {}).items():
            if value and key != "error":
                seen.setdefault(key, {})[str(value)] = None
    software.update({key: value for key, value in records.software.items() if value})
    versions = {key: " / ".join(values) for key, values in seen.items()}
    mixed = sorted(key for key, values in seen.items() if len(values) > 1)
    if not versions:
        versions = export.versions()
    text = software_text(software, versions) + "."
    if mixed:
        text += f" Versions differ between runs for: {', '.join(mixed)}."
    return section.paragraph(text)


def citations_section(bundles=(), used=()):
    section = _section("citations")
    items = [export.CITATIONS["boes2015"], export.CITATIONS["fox2018"]]
    for bundle in bundles:
        if bundle.get("citation"):
            items.append(f"Connectome: {bundle['citation']}")
    items += [METHOD_CITATIONS["fedorov2012"]] + [METHOD_CITATIONS[key] for key in ("winkler2014", "benjamini1995", "smith2009", "brett2001") if key in used]
    items += [export.CITATIONS[key] for key in export.DEBATE_CITATIONS]
    return section.bullets(list(dict.fromkeys(items)))


def acknowledgements_section(bundles):
    """The data use acknowledgements of the bundles (the GSP wording for GSP-DUT-2014 bundles), or None."""
    texts = []
    for bundle in bundles:
        text = bundle.get("acknowledgement") or (export.GSP_ACKNOWLEDGEMENT if bundle.get("licence") == export.GSP_LICENCE else None)
        if text and text not in texts:
            texts.append(text)
    if not texts:
        return None
    section = _section("acknowledgements")
    for text in texts:
        section.paragraph(text)
    return section


def disclaimer_section():
    return _section("disclaimer").paragraph(export.DISCLAIMER)


# One exported run (export.methods_text)


def _top_pct_text(rule, resolved):
    """Methods sentence of a top-x% rule: a rank rule (no p-value, not an effect size) restricted to the
    rule's sign, as thresholds.describe and the sidecar's "derived" field state it ("top 5% of T (≥ 2.28)").
    A one-sided rule is written with its signed cut (T ≥ 2.28, or T ≤ -1.90 for the negative tail), never as |T|.
    """
    statistic = resolved.get("statistic") or rule.get("statistic") or "T"
    name = "T" if statistic == "T" else "mean Fz"
    sign = rule.get("sign", "both")
    value = resolved.get("value")
    x = f"{float(rule.get('value', 0)):g}"
    if sign == "pos":
        values, cut = f"positive {name} values", (f"{name} ≥ {value:.3g}" if value is not None else None)
    elif sign == "neg":
        values, cut = f"negative {name} values", (f"{name} ≤ -{value:.3g}" if value is not None else None)
    else:
        values, cut = f"absolute {name} values", (f"|{name}| ≥ {value:.3g}" if value is not None else None)
    cut = cut or "no voxel of this sign, nothing passes"
    df = f"df = {resolved['df']}, " if statistic == "T" and resolved.get("df") is not None else ""
    maps = "T maps" if statistic == "T" else "Mean Fz maps"
    return f"{maps} were thresholded at the top {x}% of {values} ({cut}; rank rule top_pct, no p-value; {df}V = {resolved['v']:,} voxels); maps are stored unthresholded."


def run_sections(info, n, subset, threshold, lesions, run_meta=None, software=None):
    """Sections of one exported network-mapping run (the content of ``export.methods_text``)."""
    run_meta = run_meta or {}
    null = run_meta.get("null") or {}
    resolved = threshold["resolved"]
    spacing = info.get("voxel_size_mm") or []
    spacing_text = "x".join(f"{v:g}" for v in spacing) + " mm" if spacing else "connectome"
    subject_list = export._subject_list(run_meta, partial=subset["partial"])
    if subset["preview"]:
        subset_text = f" (preview: a {'random ' if subset['random'] else ''}subset of {n} of {info.get('n_subjects')} subjects{', seed ' + str(subset['seed']) if subset['random'] else ''})"
    elif subset["partial"]:
        subset_text = f" (partial run: {n} of {info.get('n_subjects')} subjects)"
    elif subject_list["name"]:
        subset_text = f" (the named subject subset \"{subject_list['name']}\" of the connectome's {info.get('n_subjects')} subjects)"
    elif subject_list["sha256"]:
        subset_text = f" (a subject list of {n} of {info.get('n_subjects')} subjects)"
    else:
        subset_text = ""
    # Connectome identity, when known: DOI, dataset version, named subset and global signal regression
    identity = []
    if info.get("doi"):
        identity.append(f"doi:{info['doi']}")
    if info.get("dataset_version"):
        identity.append(f"dataset version {info['dataset_version']}")
    if subject_list["name"]:
        identity.append(f"subset \"{subject_list['name']}\"")
    if info.get("gsr") is True:
        identity.append("preprocessed with global signal regression")
    elif info.get("gsr") is False:
        identity.append("preprocessed without global signal regression")
    identity_text = f"; {', '.join(identity)}" if identity else ""
    rule = threshold.get("rule") or resolved.get("rule") or {}
    if rule.get("kind") == "top_pct":
        threshold_text = _top_pct_text(rule, resolved)
    elif resolved["statistic"] == "Fz":
        threshold_text = f"Maps were displayed at |mean Fz| ≥ {resolved['value']:g} (effect size; maps are stored unthresholded)."
    else:
        p_text = f", two-sided p_unc ≤ {resolved['p_unc']:.2g}" if resolved.get("p_unc") is not None else ""
        threshold_text = f"T maps were thresholded at |T| ≥ {export._format_number(resolved['value'])} (rule {threshold['rule']['kind']}, df = {resolved['df']}, V = {resolved['v']:,} voxels{p_text}); maps are stored unthresholded."
    qa_text = "Connectome-confound QA followed van den Heuvel et al. (2026): the spatial correlation between each T map and the connectome degree map was computed when the degree map was available"
    if null.get("k"):
        qa_text += (
            f", and each lesion was rigidly relocated to {null['k']} random positions inside the brain mask "
            f"(integer voxel shifts on the connectome grid, ≥ {int(100 * null.get('min_inside', 0.95))}% of the lesion weight inside, overlap with the original lesion allowed, seed {null.get('seed', 0)}) "
            "and mapped in the same pass"
        )
        if any((lesion.get("qa") or {}).get("null") for lesion in lesions):
            qa_text += "; the similarity between each map and the maps of its relocated lesions is reported"
        else:
            qa_text += "; the similarity to the relocated-lesion maps is not available for this run (it is computed when a run completes)"
    qa_text += "."
    paragraph = (
        f"Lesion network maps were computed with the lesion network mapping library of 3D Slicer ({export.LIBRARY_NAME} {export.LIBRARY_VERSION}). "
        f"Each lesion was resampled onto the {spacing_text} grid of the {info.get('name')} normative functional connectome ({info.get('n_voxels', 0):,} voxels{identity_text}) as fractional voxel occupancy. "
        f"For each of N = {n} subjects{subset_text}, the occupancy-weighted time course of the lesion was correlated (Pearson r) with the time course of every voxel, "
        "the correlations were averaged over runs and Fisher z-transformed, and maps of the mean r, the mean Fisher z and the voxelwise one-sample t statistic of Fisher z "
        f"across subjects (df = {max(n - 1, 0)}) were computed (Boes et al., 2015; Fox, 2018). {threshold_text} {qa_text}"
    )
    sections = [_section("maps").paragraph(paragraph)]
    parameters = _section("parameters").table(
        ["Parameter", "Value"],
        [
            ["Connectome", f"{info.get('name')} (manifest sha256 {info.get('manifest_sha256') or 'n/a'})"],
            ["Connectome DOI and version", f"{('doi:' + info['doi']) if info.get('doi') else NOT_RECORDED}; version {info.get('dataset_version') or NOT_RECORDED}"],
            ["Global signal regression", "yes" if info.get("gsr") is True else "no" if info.get("gsr") is False else NOT_RECORDED],
            ["Connectome mask sha256", info.get("mask_sha256") or NOT_RECORDED],
            ["Data use terms", info.get("licence") or NOT_RECORDED],
            ["Subjects (N)", f"{n} of {info.get('n_subjects')}"],
            ["Subset", f"{'preview' if subset['preview'] else 'partial' if subset['partial'] else 'subject list' if subject_list['sha256'] else 'all'}; random {str(subset['random']).lower()}; seed {subset['seed']}"],
            ["Subject list", export._subject_list_text(subject_list)],
            ["Threshold rule", f"`{json.dumps(threshold['rule'], sort_keys=True)}`"],
            ["Resolved threshold", threshold["derived"]],
            ["Relocated-lesion null", f"{null.get('k', 0)} per lesion, seed {null.get('seed', 0)}"],
        ],
    )
    sections.append(parameters)
    qa_rows = []
    for lesion in lesions:
        qa = lesion.get("qa") or {}
        null_summary = qa.get("null") or {}
        voxels = lesion.get("voxels_on_connectome_grid")
        qa_rows.append([
            lesion["name"],
            voxels if voxels is not None else "n/a",
            export._format_number(lesion.get("fractional_sum"), 1),
            export._format_number(qa.get("degree_r")),
            export._format_number(qa.get("degree_rho")),
            export._format_number(null_summary.get("r_median")),
            export._format_number(null_summary.get("r_mean_map")),
            export._format_number(null_summary.get("percentile"), 0),
        ])
    sections.append(_section("lesion_qa").table(["Lesion", "Voxels on connectome grid", "Fractional sum", "Degree r", "Degree rho", "Null r (median)", "Null r (mean map)", "Null percentile"], qa_rows))
    software = {**(run_meta.get("software") or {}), **(software or {})}
    sections.append(_section("software").paragraph(software_text(software, run_meta.get("versions") or export.versions()) + "."))
    sections.append(citations_section([info]))
    acknowledgements = acknowledgements_section([info])
    if acknowledgements is not None:
        sections.append(acknowledgements)
    sections.append(disclaimer_section())
    return sections

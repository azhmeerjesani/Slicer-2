"""Reporting checklist (15 items) and battery strip (6 dots) derived from the run registry and files (standard library only).

Addendum §3.11 and the reviewer checklist of the 2026 debate (research notes, validation §8). Every
item is evaluated only from what was run and written: the study (``participants.tsv``, ``study.json``),
the run registry (``derivatives/lnm/runs.jsonl``) and the files of the runs (see
:class:`.methods.Records`). Each evaluation is a dict::

    {"id": "A1", "number": 1, "title", "status": "done" | "not_run" | "na" | "flagged",
     "value": short text, "evidence": [run ids, or "file:<path relative to the root>"]}

"flagged" means the step was run (or partly run) and something needs the reader's attention. Checklist
and battery warnings never block an export.

:func:`battery` maps the items to six dots: grey (not run), green (run) and amber (run and flagged, or
only partly run when a dot covers two items)::

    1 Degree/PC1 QA                                   item G9
    2 Null reference                                  item E7
    3 Specificity (label permutation + volume covariate)   item F8
    4 Validation vs baselines                         item H11
    5 Robustness (threshold + connectome)             items D6 and I13
    6 Provenance & sharing                            items B4 and J14
"""

import dataclasses
import os

from . import methods

DONE = "done"
NOT_RUN = "not_run"
NA = "na"
FLAGGED = "flagged"
STATUSES = (DONE, NOT_RUN, NA, FLAGGED)
STATUS_LABELS = {DONE: "done", NOT_RUN: "not run", NA: "N/A", FLAGGED: "flagged"}
GREY, GREEN, AMBER = "grey", "green", "amber"
CROSS_CONNECTOME_R_FLAG = 0.7
MIN_PERMUTATIONS = 1000
PUBLICATION_MANIFEST = ("derivatives", "lnm", "publication", "manifest.json")
NEUROVAULT_IMAGES = ("neurovault", "images.json")
# Files whose wording item K15 checks (generated methods and reports; user-edited copies included)
WORDING_FILES = ("methods.md", "methods.txt", "methods.html", "report.html", "report.md")
# Kinds of runs that compare a map with published maps of unrelated conditions (item G10)
CROSS_CONDITION_KINDS = ("refmaps", "crosscondition", "comparison")


@dataclasses.dataclass(frozen=True)
class Item:
    """One checklist item: ``evaluate(root, study, runs)`` gives its status dict."""

    id: str
    number: int
    title: str
    check: object  # callable(records) -> (status, value, evidence)

    def evaluate(self, root, study=None, runs=None, records=None):
        """{"id", "number", "title", "status", "value", "evidence"} of this item for ``root``."""
        records = records or methods.Records(root, study=study, runs=runs)
        status, value, evidence = self.check(records)
        return {"id": self.id, "number": self.number, "title": self.title, "status": status, "value": value, "evidence": list(dict.fromkeys(evidence))}


def _file_evidence(records, path):
    return f"file:{records.relpath(path)}"


# Item checks: (status, value, evidence)


def _a1_lesion_source(records):
    rows = records.rows
    if records.study is None or not rows:
        return NOT_RUN, "no lesions", []
    flags = []
    no_source = sum(1 for row in rows if methods._missing(row.get("source_type")))
    if no_source:
        flags.append(f"{no_source} lesion(s) without a source type")
    no_method = sum(1 for row in rows if methods._missing(row.get("trace_method")) and methods._missing(row.get("tracer")) and methods._missing(row.get("rater")))
    if no_method:
        flags.append(f"{no_method} lesion(s) without a tracing method or rater")
    normalized = [row for row in rows if not methods._missing(row.get("norm_method")) or not methods._missing(row.get("lesion_native")) or row.get("space") == "native"]
    not_passed = sum(1 for row in normalized if row.get("norm_qc") != "pass")
    if not_passed:
        flags.append(f"normalization QC not passed for {not_passed} of {len(normalized)}")
    automatic = [row for row in rows if any(token.startswith("auto:") for token in (row.get("trace_method") or "").split("+"))]
    unapproved = sum(1 for row in automatic if row.get("review_status") != "approved")
    if unapproved:
        flags.append(f"{unapproved} automatic segmentation(s) not approved")
    value = f"{len(rows)} lesions; sources {methods._counts([None if methods._missing(row.get('source_type')) else row['source_type'] for row in rows])}"
    if normalized:
        value += f"; normalization QC pass {len(normalized) - not_passed}/{len(normalized)}"
    evidence = [_file_evidence(records, os.path.join(records.study.path, "participants.tsv"))]
    return (FLAGGED, value + "; " + "; ".join(flags), evidence) if flags else (DONE, value, evidence)


def _a2_lesion_set(records):
    data = records.lesionset()
    if data is None:
        return NOT_RUN, "lesion-set diagnostics not run", []
    summary = data.get("summary") or {}
    value = f"{methods._or_nr(summary.get('n'))} lesions"
    volume = summary.get("volume") or summary.get("volumes") or {}
    if isinstance(volume, dict) and volume.get("median") is not None:
        value += f"; median volume {methods._number(volume['median'])}"
    dice = summary.get("dice") or {}
    if isinstance(dice, dict) and dice.get("median") is not None:
        value += f"; median pairwise Dice {methods._number(dice['median'])}"
    return DONE, value, [_file_evidence(records, records.path(*methods.LESIONSET_JSON))]


def _a3_control_set(records):
    rows = records.rows
    controls = [row for row in rows if row.get("group") == "control" or row.get("source_type") == "control_db"]
    control_sets = (records.study.metadata.get("control_sets") if records.study is not None else None) or []
    glms = records.group_records("glm.json", "glm")
    tests = {data.get("test") for _, data in glms} | {"continuous" for _ in records.group_records("slnm.json", "slnm")}
    if not controls and not control_sets:
        if tests and tests <= {"continuous", "one_sample"}:
            return NA, "no control comparison in the analyses (score or one-sample design)", []
        return NOT_RUN, "no control lesions", []
    value = f"{len(controls)} control lesion(s)"
    if control_sets:
        value += f"; control sets {', '.join(str(item.get('name') if isinstance(item, dict) else item) for item in control_sets)}"
    sources = methods._counts([None if methods._missing(row.get("source_type")) else row["source_type"] for row in controls])
    if sources:
        value += f"; sources {sources}"
    evidence = [run_id for run_id, data in glms if data.get("design_type") == "cases_vs_control_database" and run_id]
    database = [data for _, data in glms if data.get("design_type") == "cases_vs_control_database" and data.get("limitations")]
    if database:
        return FLAGGED, value + f"; control-database design with {len(database[0]['limitations'])} unadjustable confound(s) (see Limitations)", evidence
    return DONE, value, evidence


def _b4_connectome(records):
    groups = records.connectomes()
    if not groups:
        return NOT_RUN, "no network-mapping run", []
    missing, evidence = [], []
    for group in groups:
        bundle = group["bundle"]
        first = group["records"][0]
        evidence += [record["id"] for record in group["records"]]
        gaps = [label for label, value in (("DOI", bundle.get("doi")), ("version", bundle.get("dataset_version")), ("subject list", first.get("subject_ids_sha256") or (first.get("subject_list") or {}).get("sha256")), ("GSR", bundle.get("gsr")), ("mask", bundle.get("mask_sha256"))) if value is None]
        if gaps:
            missing.append(f"{bundle.get('name') or 'connectome'}: {', '.join(gaps)} not recorded")
    names = ", ".join(str(group["bundle"].get("name") or methods.NOT_RECORDED) for group in groups)
    return (FLAGGED, f"{names}; " + "; ".join(missing), evidence) if missing else (DONE, f"{names}: DOI, version, subject list, GSR and mask recorded", evidence)


def _c5_seed_statistic(records):
    maps = records.map_records()
    if not maps:
        return NOT_RUN, "no network-mapping run", []
    seeds = sorted({"probabilistic" if record.get("probabilistic") else "binarized" if record.get("probabilistic") is False else methods.NOT_RECORDED for record in maps})
    statistics = sorted({str(record.get("statistic") or methods.NOT_RECORDED) for record in maps})
    value = f"fractional occupancy seeds ({', '.join(seeds)} lesions); map statistic {', '.join(statistics)}"
    inputs = sorted({str((data.get("inputs") or {}).get("input")) for _, data in records.group_records("glm.json", "glm") + records.group_records("slnm.json", "slnm") if (data.get("inputs") or {}).get("input")})
    if inputs:
        value += f"; group input {', '.join(inputs)}"
    status = FLAGGED if methods.NOT_RECORDED in seeds or methods.NOT_RECORDED in statistics else DONE
    return status, value, [record["id"] for record in maps]


def _d6_thresholds(records):
    robustness = records.done_runs("robustness")
    overrides = [record["id"] for record in records.map_records() if ((record.get("threshold") or {}).get("resolved") or {}).get("allowed") is False]
    if not robustness:
        return NOT_RUN, "no threshold sweep", overrides
    flips = []
    for record in robustness:
        summary = records.summary(record) or {}
        if summary.get("flips") or summary.get("conclusion_flips"):
            flips.append(record["id"])
    evidence = [record["id"] for record in robustness] + overrides
    if flips or overrides:
        text = "conclusion flips across thresholds" if flips else "a threshold rule was applied with an override"
        return FLAGGED, text, evidence
    return DONE, f"threshold sweep run ({len(robustness)} run(s))", evidence


def _e7_overlap_null(records):
    nulls = records.done_runs("nulls")
    overlap = records.done_runs("cohort")
    if nulls:
        modes = {str((records.summary(record) or {}).get("mode") or (records.params(record) or {}).get("mode") or "") for record in nulls}
        if "fast" in modes:
            return FLAGGED, "null reference computed in the approximate (fast) mode", [record["id"] for record in nulls]
        return DONE, f"null reference: {len(nulls)} run(s)", [record["id"] for record in nulls]
    if overlap:
        return FLAGGED, "overlap maps without a null reference", [record["id"] for record in overlap]
    return NOT_RUN, "no overlap or null run", []


def _f8_design(records):
    groups = records.group_records("glm.json", "glm") + records.group_records("slnm.json", "slnm")
    if not groups:
        return NOT_RUN, "no group analysis", []
    flags = []
    for run_id, data in groups:
        label = run_id or "analysis"
        permutation = data.get("permutation") or {}
        n_perm = permutation.get("n_perm")
        if permutation.get("preview") or (isinstance(n_perm, int) and n_perm < MIN_PERMUTATIONS and not permutation.get("exhaustive")):
            flags.append(f"{label}: {n_perm} permutations")
        if data.get("volume_off_reason") or "volume_ml" not in (data.get("covariates") or []):
            flags.append(f"{label}: lesion volume covariate off")
        if data.get("warnings"):
            flags.append(f"{label}: {len(data['warnings'])} design warning(s)")
    evidence = [run_id for run_id, _ in groups if run_id]
    value = f"{len(groups)} group analysis(es); permutation inference with FWE (primary) and FDR"
    return (FLAGGED, value + "; " + "; ".join(flags), evidence) if flags else (DONE, value, evidence)


def _g9_reference_qa(records):
    maps = [record for record in records.map_records() if record.get("reference")]
    if not maps:
        return NOT_RUN, "no μC/PC1 reference QA", []
    values = [(qa or {}).get("reference") or {} for record in maps for qa in (record.get("qa") or {}).values()]
    calibrated = [value for value in values if value.get("k")]
    red = sum(1 for value in calibrated if value.get("level") == "red")
    evidence = [record["id"] for record in maps]
    if not calibrated:
        return FLAGGED, "reference QA without a null calibration (no relocated-lesion maps)", evidence
    partial = sum(1 for value in values if value.get("partial_pc1_mu") is not None)
    value = f"{len(calibrated)} map(s) calibrated against relocated lesions; partial PC1/μC similarity for {partial}"
    if red:
        return FLAGGED, value + f"; {red} more degree-like than relocated lesions", evidence
    return DONE, value, evidence


def _g10_cross_condition(records):
    runs = records.done_runs(CROSS_CONDITION_KINDS)
    if not runs:
        return NOT_RUN, "no comparison with maps of unrelated conditions", []
    return DONE, f"{len(runs)} comparison run(s)", [record["id"] for record in runs]


def _h11_validation(records):
    runs = records.done_runs("validation")
    if not runs:
        return NOT_RUN, "no out-of-sample validation", []
    without = [record["id"] for record in runs if not (records.summary(record) or {}).get("baselines")]
    evidence = [record["id"] for record in runs]
    if without:
        return FLAGGED, "validation without baselines recorded", evidence
    return DONE, f"{len(runs)} validation run(s) with baselines", evidence


def _h12_replication(records):
    evidence = []
    for record in records.done_runs("validation"):
        summary = records.summary(record) or {}
        params = records.params(record) or {}
        if str(summary.get("scheme") or params.get("scheme") or "").lower() in ("lodo", "external", "independent"):
            evidence.append(record["id"])
    for run_id, data in records.group_records("slnm.json", "slnm"):
        if (data.get("lodo") or {}).get("computed"):
            evidence.append(run_id or "slnm")
    for record in records.done_runs("robustness"):
        if (records.summary(record) or {}).get("replication"):
            evidence.append(record["id"])
    if not evidence:
        return NOT_RUN, "no independent cohort or leave-one-dataset-out replication", []
    return DONE, f"replication in {len(evidence)} run(s)", evidence


def _i13_connectome_robustness(records):
    maps = records.map_records()
    manifests = {(record.get("bundle") or {}).get("manifest_sha256") for record in maps} - {None}
    second = len(manifests) >= 2
    split_half = any(record.get("split_half") for record in maps)
    low_r, evidence = [], []
    for record in records.done_runs("robustness"):
        summary = records.summary(record) or {}
        if summary.get("connectomes") and (summary.get("connectomes") if isinstance(summary.get("connectomes"), int) else len(summary["connectomes"])) >= 2:
            second = True
            evidence.append(record["id"])
        if summary.get("split_half") is not None:
            split_half = True
            evidence.append(record["id"])
        r = methods._as_float(summary.get("cross_connectome_r"))
        if r is not None and r < CROSS_CONNECTOME_R_FLAG:
            low_r.append(f"{r:.2f}")
    evidence += [record["id"] for record in maps if record.get("split_half")]
    if second:
        evidence += [record["id"] for record in maps]
    if not second and not split_half:
        return NOT_RUN, "no second connectome or split-half", []
    parts = [("second connectome" if second else "no second connectome"), ("split-half" if split_half else "no split-half")]
    if low_r:
        return FLAGGED, ", ".join(parts) + f"; cross-connectome r {', '.join(low_r)} < {CROSS_CONNECTOME_R_FLAG}", evidence
    return (DONE if second and split_half else FLAGGED), ", ".join(parts), evidence


def _j14_sharing(records):
    manifest_path = records.path(*PUBLICATION_MANIFEST)
    manifest = methods._read_json(manifest_path)
    neurovault = [path for path in (records.path(*NEUROVAULT_IMAGES), records.path("derivatives", "lnm", "publication", *NEUROVAULT_IMAGES)) if os.path.isfile(path)]
    if manifest is None and not neurovault:
        return NOT_RUN, "no publication manifest or NeuroVault folder", []
    evidence = ([_file_evidence(records, manifest_path)] if manifest is not None else []) + [_file_evidence(records, path) for path in neurovault]
    missing = (manifest or {}).get("missing") or []
    if manifest is None:
        return FLAGGED, "NeuroVault metadata without a publication manifest", evidence
    if missing:
        return FLAGGED, f"publication manifest; missing {', '.join(missing)}", evidence
    return DONE, f"publication manifest with {len(manifest.get('files') or [])} file(s)", evidence


def _k15_wording(records):
    found, violations = [], []
    for folder, folders, names in os.walk(records.path("derivatives")):
        # Pruned by folder name below derivatives/ (never by the study's own absolute path)
        folders[:] = sorted(name for name in folders if name != "cache" and not name.startswith("."))
        for name in sorted(names):
            if name in WORDING_FILES:
                path = os.path.join(folder, name)
                found.append(path)
                try:
                    with open(path, encoding="utf-8") as f:
                        bad = methods.lint_wording(f.read())
                except (OSError, UnicodeDecodeError):
                    continue
                if bad:
                    violations.append(f"{records.relpath(path)}: {', '.join(sorted(set(bad)))}")
    if not found:
        return NOT_RUN, "no generated methods or report", []
    evidence = [_file_evidence(records, path) for path in found]
    if violations:
        return FLAGGED, "banned wording: " + "; ".join(violations), evidence
    return DONE, f"{len(found)} file(s) use the neutral wording and cite both sides of the debate", evidence


ITEMS = (
    Item("A1", 1, "Lesion source, tracing and normalization QC", _a1_lesion_source),
    Item("A2", 2, "Lesion-set properties", _a2_lesion_set),
    Item("A3", 3, "Control set", _a3_control_set),
    Item("B4", 4, "Connectome identity (DOI, version, subject list, GSR, mask)", _b4_connectome),
    Item("C5", 5, "Seed and statistic", _c5_seed_statistic),
    Item("D6", 6, "Thresholds and robustness", _d6_thresholds),
    Item("E7", 7, "Overlap and null", _e7_overlap_null),
    Item("F8", 8, "Design, permutation and correction", _f8_design),
    Item("G9", 9, "μC/PC1 with null and partial", _g9_reference_qa),
    Item("G10", 10, "Cross-condition repetition", _g10_cross_condition),
    Item("H11", 11, "Out-of-sample vs baselines", _h11_validation),
    Item("H12", 12, "Independent replication", _h12_replication),
    Item("I13", 13, "Second connectome and split-half", _i13_connectome_robustness),
    Item("J14", 14, "Sharing", _j14_sharing),
    Item("K15", 15, "Interpretation wording", _k15_wording),
)
ITEMS_BY_ID = {item.id: item for item in ITEMS}

BATTERY = (
    ("degree_qa", "Degree/PC1 QA", ("G9",)),
    ("null_reference", "Null reference", ("E7",)),
    ("specificity", "Specificity (label permutation + volume covariate)", ("F8",)),
    ("validation", "Validation vs baselines", ("H11",)),
    ("robustness", "Robustness (threshold + connectome)", ("D6", "I13")),
    ("provenance", "Provenance & sharing", ("B4", "J14")),
)


def evaluate(root, study=None, runs=None, records=None):
    """The 15 item evaluations of ``root`` (in checklist order)."""
    records = records or methods.Records(root, study=study, runs=runs)
    return [item.evaluate(root, records=records) for item in ITEMS]


def dot_colour(statuses):
    """Battery colour of the statuses of a dot's items (N/A items are ignored): grey when none was
    run, amber when one is flagged or only some were run, green when all were run.
    """
    statuses = [status for status in statuses if status != NA]
    if not statuses or all(status == NOT_RUN for status in statuses):
        return GREY
    if all(status == DONE for status in statuses):
        return GREEN
    return AMBER


def battery(root, study=None, runs=None, items=None, records=None):
    """Six battery dots: [{"id", "label", "colour": grey|green|amber, "items": [ids], "text"}]."""
    items = items if items is not None else evaluate(root, study=study, runs=runs, records=records)
    by_id = {item["id"]: item for item in items}
    out = []
    for dot_id, label, item_ids in BATTERY:
        members = [by_id[item_id] for item_id in item_ids if item_id in by_id]
        colour = dot_colour([member["status"] for member in members])
        text = "; ".join(f"{member['id']} {STATUS_LABELS[member['status']]}: {member['value']}" for member in members)
        out.append({"id": dot_id, "label": label, "colour": colour, "items": list(item_ids), "text": text})
    return out


def checklist_markdown(items, dots=None, title="Reporting checklist"):
    """checklist.md: the 15 items as a table (and the battery when given), with the disclaimer."""
    from .export import DISCLAIMER

    def cell(text):
        return str(text).replace("|", "\\|").replace("\n", " ")

    lines = [f"# {title}", "", "| # | Item | Status | Value | Evidence |", "|---|---|---|---|---|"]
    for item in items:
        evidence = ", ".join(item["evidence"]) or "none"
        lines.append(f"| {item['id']} | {cell(item['title'])} | {STATUS_LABELS[item['status']]} | {cell(item['value'])} | {cell(evidence)} |")
    if dots:
        lines += ["", "## Battery", "", "| Check | Colour | Items |", "|---|---|---|"]
        lines += [f"| {cell(dot['label'])} | {dot['colour']} | {cell(dot['text'])} |" for dot in dots]
    lines += ["", "Checklist and battery warnings never block an export.", "", "## Disclaimer", "", DISCLAIMER, ""]
    return "\n".join(lines)


def write_checklist(root, out_dir, study=None, runs=None, records=None):
    """Write <out_dir>/checklist.md; return (path, items, dots)."""
    from .export import _write_text_atomic

    records = records or methods.Records(root, study=study, runs=runs)
    items = evaluate(root, records=records)
    dots = battery(root, items=items)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "checklist.md")
    _write_text_atomic(path, checklist_markdown(items, dots))
    return path, items, dots

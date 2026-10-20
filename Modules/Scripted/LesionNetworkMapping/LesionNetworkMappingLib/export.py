"""Export of lesion network mapping results: maps, JSON sidecars and run provenance.

A run is exported to one folder (typically ``<study>/derivatives/lnm/conn-<label>[_n-<N>]/``)::

    <lesion>_conn-<label>_desc-{AvgR,AvgRFz,T}[PreviewN<k>]_funcmap.nii.gz   maps (Lead-DBS names; float32,
                                                                            exact connectome header; the
                                                                            T map has intent code 3 and df)
    <lesion>_conn-<label>[_desc-PreviewN<k>]_lnm.json                       sidecar per lesion
    provenance.json                                                         everything about the run
    methods.md                                                              generated methods paragraph
    reproduce.sh                                                            PythonSlicer command line

Every sidecar, ``methods.md`` and ``provenance.json`` carries the research-use disclaimer
(:data:`DISCLAIMER`), N, df, the subset/preview flag and seed, the threshold rule and its resolved value,
and the connectome's identity when known: DOI, dataset version, GSR, mask sha256, named subject subset
and data use terms (a GSP-derived map carries "terms": "GSP-DUT-2014" and the GSP acknowledgement).
All files are plain NIfTI-1, JSON, Markdown and shell text.

:func:`check_poolable` and :func:`compare_runs` tell whether maps of several runs may be pooled
(same connectome manifest, subject list, statistic, resampling and library version; see
:func:`compatibility_key`) and how two runs differ.
"""

import datetime
import hashlib
import json
import math
import os
import platform
import re
import shlex
import shutil
import sys

# SPEC_LNM_ADDENDUM section 3.11 (exact text): both sides of the 2026 debate are cited
DISCLAIMER = (
    "Research use only — not for clinical decisions. Lesion network mapping specificity is debated "
    "(van den Heuvel et al., 2026, Nat Neurosci, doi:10.1038/s41593-025-02196-7; "
    "Siddiqi et al., 2026, bioRxiv, doi:10.64898/2026.02.24.707529; "
    "Zalesky & Cash, 2026, Nat Neurosci, doi:10.1038/s41593-026-02259-3)."
)
CITATIONS = {
    "boes2015": "Boes AD, Prasad S, Liu H, et al. Network localization of neurological symptoms from focal brain lesions. Brain. 2015;138(10):3061-3075. doi:10.1093/brain/awv228",
    "fox2018": "Fox MD. Mapping symptoms to brain networks with the human connectome. N Engl J Med. 2018;379(23):2237-2245. doi:10.1056/NEJMra1706158",
    "vandenheuvel2026": "van den Heuvel et al. Nat Neurosci. 2026. doi:10.1038/s41593-025-02196-7",
    "siddiqi2026": "Siddiqi SH, Horn A, Schaper FLWVJ, et al. The methodological foundations of lesion network mapping remain sound. bioRxiv. 2026. doi:10.64898/2026.02.24.707529",
    "zaleskycash2026": "Zalesky A, Cash RFH. Null models for lesion network mapping. Nat Neurosci. 2026;29:1025-1028. doi:10.1038/s41593-026-02259-3",
}
# Citations of the specificity debate, listed after the methods and connectome citations
DEBATE_CITATIONS = ("vandenheuvel2026", "siddiqi2026", "zaleskycash2026")
GSP_LICENCE = "GSP-DUT-2014"
# Acknowledgement wording required by the GSP Open Access Data Use Terms (2014-Apr-22), item 6a
GSP_ACKNOWLEDGEMENT = (
    "Data were provided [in part] by the Brain Genomics Superstruct Project of Harvard University and the Massachusetts General Hospital, "
    "(Principal Investigators: Randy Buckner, Joshua Roffman, and Jordan Smoller), with support from the Center for Brain Science "
    "Neuroinformatics Research Group, the Athinoula A. Martinos Center for Biomedical Imaging, and the Center for Human Genetic Research. "
    "20 individual investigators at Harvard and MGH generously contributed data to the overall project."
)
NOT_RECORDED = "not recorded"
LIBRARY_NAME = "LesionNetworkMappingLib"
LIBRARY_VERSION = "1.0"
SIDECAR_FORMAT = "slicer-lnm-sidecar"
PROVENANCE_FORMAT = "slicer-lnm-provenance"
RUN_FORMAT = "slicer-lnm-run"
_PREVIEW_SUFFIX = re.compile(r"PreviewN(\d+)")
# Engine options whose value is a path (made absolute in reproduce.sh)
_PATH_OPTIONS = ("--bundle", "--lesion", "--brain-mask")


# Hashes, versions and small file helpers


def file_sha256(path, block=1 << 20):
    """Hex sha256 of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(block):
            digest.update(chunk)
    return digest.hexdigest()


def subjects_sha256(subject_ids):
    """Hex sha256 of a subject list: the UTF-8 ids, each followed by a newline, in run order."""
    return hashlib.sha256("".join(f"{subject_id}\n" for subject_id in subject_ids).encode("utf-8")).hexdigest()


def now_iso():
    """Current UTC time as ISO 8601 text with seconds."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def versions():
    """Versions of Python, numpy, scipy, the BLAS library and this library."""
    out = {"python": platform.python_version(), LIBRARY_NAME: LIBRARY_VERSION}
    for module_name in ("numpy", "scipy"):
        module = sys.modules.get(module_name)
        if module is None:
            try:
                module = __import__(module_name)
            except ImportError:
                continue
        out[module_name] = getattr(module, "__version__", "unknown")
    try:
        import numpy as np

        blas = np.show_config(mode="dicts")["Build Dependencies"]["blas"]
        out["blas"] = f"{blas.get('name', 'unknown')} {blas.get('version', '')}".strip()
    except Exception:
        pass
    return out


def host_info():
    """Operating system, architecture and CPU count (no host name)."""
    return {"system": platform.system(), "release": platform.release(), "machine": platform.machine(), "cpus": os.cpu_count()}


def write_json_atomic(path, obj):
    """Write JSON (UTF-8, indented) through a temporary file and an atomic rename."""
    _write_text_atomic(path, json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def _write_text_atomic(path, text, mode=None):
    path = os.fspath(path)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def _clean(value):
    """Make a value JSON-compatible (numpy scalars and arrays, tuples, non-finite floats as None)."""
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        return _clean(value.tolist())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# Names


def connectome_label(bundle_name):
    """BIDS-style label of a connectome (letters and digits of its name), used in "conn-<label>"."""
    from .engine import connectome_label as label

    return label(bundle_name)


def preview_suffix(preview_n):
    """The "desc" suffix of a preview (subset) run: "PreviewN<k>", or "" when not a preview."""
    return f"PreviewN{int(preview_n)}" if preview_n else ""


def map_paths(out_dir, lesion_name, bundle_name, preview_n=None, desc_suffix=None):
    """Map file names of one lesion: {"AvgR", "AvgFz", "T": path} (see ``engine.output_paths``).

    :param desc_suffix: "desc" suffix to use instead of the preview one (e.g. "SubsetFirst100Subjects").
    """
    from .engine import output_paths

    return output_paths(out_dir, lesion_name, bundle_name, preview_suffix(preview_n) if desc_suffix is None else desc_suffix)


def sidecar_path(out_dir, lesion_name, bundle_name, preview_n=None, desc_suffix=None):
    """Sidecar file name of one lesion: <lesion>_conn-<label>[_desc-PreviewN<k>]_lnm.json (or ``desc_suffix``)."""
    suffix = preview_suffix(preview_n) if desc_suffix is None else desc_suffix
    desc = f"_desc-{suffix}" if suffix else ""
    return os.path.join(out_dir, f"{lesion_name}_conn-{connectome_label(bundle_name)}{desc}_lnm.json")


def run_dir_name(bundle_name, n=None):
    """Folder name of a run in a study's derivatives: conn-<label>[_n-<N>]."""
    return f"conn-{connectome_label(bundle_name)}" + (f"_n-{int(n)}" if n else "")


def derivatives_dir(study_path, bundle_name, n=None):
    """<study>/derivatives/lnm/conn-<label>[_n-<N>]."""
    return os.path.join(os.fspath(study_path), "derivatives", "lnm", run_dir_name(bundle_name, n))


def group_map_path(out_dir, bundle_name, desc):
    """Cohort map file name: group_conn-<label>_desc-<desc>_map.nii.gz (desc: overlapCount, sens90, specT, ...)."""
    if not re.fullmatch(r"[A-Za-z0-9]+", desc):
        raise ValueError(f"A desc label must contain only letters and digits: {desc!r}")
    return os.path.join(out_dir, f"group_conn-{connectome_label(bundle_name)}_desc-{desc}_map.nii.gz")


def write_peaks_tsv(path, peaks, labels=None):
    """Write a peak table (``stats.peak_table`` rows) as TSV with columns x, y, z (world mm), value and label."""
    lines = ["x\ty\tz\tvalue\tlabel"]
    for index, peak in enumerate(peaks):
        label = labels[index] if labels is not None and index < len(labels) and labels[index] else "n/a"
        x, y, z = peak["world"]
        lines.append(f"{x:.1f}\t{y:.1f}\t{z:.1f}\t{peak['value']:.4g}\t{label}")
    _write_text_atomic(path, "\n".join(lines) + "\n")
    return path


def _preview_from_suffix(desc_suffix):
    match = _PREVIEW_SUFFIX.fullmatch(desc_suffix or "")
    return int(match.group(1)) if match else None


# Run records


def data_sha256(metadata):
    """Hex sha256 of the keys of connectome.json that define the data (format, version, grid, subjects
    and dtype), as canonical JSON: unlike the manifest sha256 it does not change when only optional
    metadata (provenance, description, subsets) is edited.
    """
    from .connectome import REQUIRED_KEYS

    data = {key: metadata.get(key) for key in REQUIRED_KEYS if key != "name"}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def bundle_info(bundle):
    """JSON-compatible description of a connectome bundle (also accepts an existing info dict).

    Besides the identity of the bundle (name, manifest sha256, grid) it holds the provenance of
    connectome.json when known (None otherwise): doi, dataset_version, licence, terms_sha256,
    acknowledgement, gsr, preprocessing, population, mask_sha256 and the subset names. An invalid
    optional key (malformed "subsets", mismatched "mask.sha256") gives None and a "warnings" entry.
    """
    if isinstance(bundle, dict):
        return dict(bundle)
    manifest = os.path.join(bundle.path, "connectome.json")
    metadata = getattr(bundle, "metadata", {})
    provenance = bundle.provenance if hasattr(bundle, "provenance") else {}
    source = provenance.get("source") or {}
    preprocessing = provenance.get("preprocessing") or {}
    licence = provenance.get("licence")
    warnings = []

    def optional(attribute, default):
        # An invalid optional key written by another tool (e.g. a malformed "subsets") is reported, not fatal
        try:
            return getattr(bundle, attribute, default)
        except ValueError as exc:
            warnings.append(str(exc))
            return None

    mask_sha256 = optional("mask_sha256", None)
    subsets = optional("subsets", {})
    info = {
        "name": bundle.name,
        "label": connectome_label(bundle.name),
        "path": bundle.path,
        "format_version": metadata.get("version"),
        "manifest_sha256": file_sha256(manifest) if os.path.isfile(manifest) else None,
        "data_sha256": data_sha256(metadata) if metadata else None,
        "description": bundle.description,
        "citation": bundle.citation,
        "space": metadata.get("space"),
        "n_subjects": bundle.n_subjects,
        "n_voxels": bundle.n_voxels,
        "shape": list(bundle.shape),
        "affine": _clean(bundle.affine),
        "voxel_size_mm": [round(float(v), 6) for v in bundle.grid.spacing],
        "doi": source.get("doi"),
        "dataset_version": source.get("version"),
        "licence": licence if licence and licence != "unknown" else None,
        "terms_sha256": provenance.get("termsSha256"),
        "acknowledgement": provenance.get("acknowledgement") or (GSP_ACKNOWLEDGEMENT if licence == GSP_LICENCE else None),
        "gsr": preprocessing.get("gsr") if preprocessing.get("gsr") in (True, False) else None,
        "preprocessing": _clean({key: value for key, value in preprocessing.items() if value is not None and value != "unknown"}) or None,
        "population": _clean({key: value for key, value in (provenance.get("population") or {}).items() if value is not None}) or None,
        "mask_sha256": mask_sha256,
        "subsets": None if subsets is None else list(subsets),
    }
    if warnings:
        info["warnings"] = warnings
    return info


def run_record(argv, bundle, subject_ids, args=None, rule=None, subject_list=None):
    """Initial run record written by the engine's --run-json (status "running"; completed at the end).

    :param subject_list: the ``connectome.SubjectList`` of a --subject-list run: its name (for a named
      subset), source and sha256 are recorded in "subjects" (list_name, list_source, list_file, list_sha256).
    """
    subjects = {
        "n": len(subject_ids),
        "requested": getattr(args, "subjects", None),
        "random": bool(getattr(args, "random", False)),
        "seed": getattr(args, "seed", 0),
    }
    if subject_list is not None:
        subjects.update({
            "list_name": subject_list.name,
            "list_source": subject_list.source,
            "list_file": subject_list.path,
            "list_sha256": subject_list.sha256,
        })
    record = {
        "format": RUN_FORMAT,
        "version": 1,
        "status": "running",
        "argv": list(argv),
        "cwd": os.getcwd(),
        "bundle": bundle_info(bundle),
        "subjects": subjects,
        "subject_ids_sha256": subjects_sha256(subject_ids),
        "seed": getattr(args, "seed", 0),
        "desc_suffix": getattr(args, "desc_suffix", ""),
        "threshold_rule": rule,
        "supersample": getattr(args, "supersample", None),
        "probabilistic": bool(getattr(args, "probabilistic", False)),
        "threads": getattr(args, "threads", None),
        "versions": versions(),
        "host": host_info(),
        "start": now_iso(),
        "end": None,
    }
    return record


# Pooling guard


def _argv_value(argv, option):
    """Value of ``option`` in an argument list ("--opt value" or "--opt=value"), or None."""
    argv = list(argv or [])
    for index, token in enumerate(argv):
        if token == option and index + 1 < len(argv):
            return argv[index + 1]
        if token.startswith(option + "="):
            return token.split("=", 1)[1]
    return None


def _run_bundle(run_meta):
    """The connectome info of a run record (run.json "bundle") or of a provenance.json ("connectome")."""
    return (run_meta or {}).get("bundle") or (run_meta or {}).get("connectome") or {}


def compatibility_key(run_meta):
    """What must be equal for maps of two runs to be pooled in one analysis:
    (manifest_sha256, subject_ids_sha256, statistic, supersample, probabilistic, library version).

    :param run_meta: an engine run record (run.json) or an exported provenance.json. Missing values
      are None, except the defaults of older records (statistic "T", supersample 2, binarized lesions).
    """
    run_meta = run_meta or {}
    argv = run_meta.get("argv") or (run_meta.get("run") or {}).get("argv") or []
    supersample = run_meta.get("supersample")
    if supersample is None:
        value = _argv_value(argv, "--supersample")
        supersample = int(value) if value and value.isdigit() else 2
    probabilistic = run_meta.get("probabilistic")
    if probabilistic is None:
        probabilistic = "--probabilistic" in argv
    library_version = (run_meta.get("versions") or {}).get(LIBRARY_NAME) or (run_meta.get("software") or {}).get(LIBRARY_NAME)
    return (
        _run_bundle(run_meta).get("manifest_sha256"),
        run_meta.get("subject_ids_sha256"),
        run_meta.get("statistic") or "T",
        int(supersample),
        bool(probabilistic),
        library_version,
    )


def _short(value):
    return "not recorded" if value is None else str(value)[:12] if isinstance(value, str) and len(value) == 64 else str(value)


def _differences(label, values, formatter=_short, prefix=""):
    """'<label> (<prefix>a, b)' when the values differ (each shown once, in order of appearance), else None."""
    distinct = list(dict.fromkeys(values))
    if len(distinct) < 2:
        return None
    return f"{label} ({prefix}{', '.join(formatter(value) for value in distinct)})"


def check_poolable(run_metas):
    """Reasons why maps of these runs must not be pooled (an empty list when they can be).

    Runs are poolable when their :func:`compatibility_key` values are equal: the same connectome
    manifest, the same subject list, the same statistic, the same lesion resampling settings and the
    same library version. Runs that did not finish (stopped, partial exports or still running) and runs
    whose subject list is not recorded are never poolable. Messages are human readable, e.g.
    "different subject lists (sha 1a2b3c4d5e6f, ...)".
    """
    run_metas = [run_meta or {} for run_meta in run_metas]
    if len(run_metas) < 2:
        return []
    keys = [compatibility_key(run_meta) for run_meta in run_metas]
    messages = []
    # A stopped (partial) run used fewer subjects than its list; its subject identity is not comparable
    unfinished = [(index, _run_unfinished(run_meta)) for index, run_meta in enumerate(run_metas, 1)]
    unfinished = [(index, status) for index, status in unfinished if status]
    if unfinished:
        messages.append("run did not finish (" + "; ".join(f"run {index}: {status}" for index, status in unfinished) + ")")
    manifests = [key[0] for key in keys]
    if len(set(manifests)) > 1:
        message = _differences("different connectome manifest", manifests, prefix="sha ")
        data = [_run_bundle(run_meta).get("data_sha256") for run_meta in run_metas]
        if None not in data and len(set(data)) == 1:
            message += "; the connectome data are the same, only connectome.json metadata differ"
        messages.append(message)
    subject_lists = [key[1] for key in keys]
    missing = [index for index, value in enumerate(subject_lists, 1) if value is None]
    if missing:
        runs = ", ".join(f"run {index}" for index in missing)
        messages.append(f"subject list not recorded ({runs}: partial, stopped or older run)")
    elif len(set(subject_lists)) > 1:
        messages.append(_differences("different subject lists", subject_lists, prefix="sha "))
    for index, label in ((2, "different statistics"), (5, "different library versions")):
        text = _differences(label, [key[index] for key in keys])
        if text:
            messages.append(text)
    resampling = [f"supersample {key[3]}, {'probabilistic' if key[4] else 'binarized'} lesions" for key in keys]
    text = _differences("different resampling settings", resampling, str)
    if text:
        messages.append(text)
    return messages


def compare_runs(a, b):
    """Banners describing how two runs differ (an empty list when they are comparable as they stand).

    Includes the :func:`check_poolable` reasons and differences of connectome provenance that do not
    prevent pooling by themselves but matter when comparing maps: GSR, dataset version, DOI, mask, N and subset.
    """
    banners = check_poolable([a, b])
    info_a, info_b = _run_bundle(a), _run_bundle(b)

    def describe(value):
        return NOT_RECORDED if value is None else "yes" if value is True else "no" if value is False else str(value)

    for key, label in (("gsr", "different GSR"), ("dataset_version", "different dataset version"), ("doi", "different connectome DOI"), ("mask_sha256", "different connectome mask")):
        values = [info_a.get(key), info_b.get(key)]
        if values[0] != values[1]:
            shown = [_short(value) if key == "mask_sha256" else describe(value) for value in values]
            banners.append(f"{label} ({shown[0]} vs {shown[1]})")
    n_values = [_run_n(run_meta) for run_meta in (a, b)]
    if n_values[0] != n_values[1]:
        banners.append(f"different N ({describe(n_values[0])} vs {describe(n_values[1])})")
    names = [_subject_list(run_meta).get("name") for run_meta in (a, b)]
    if names[0] != names[1]:
        banners.append(f"different subject subsets ({names[0] or 'none'} vs {names[1] or 'none'})")
    return banners


def _run_unfinished(run_meta):
    """Why a run record (run.json or provenance.json) is not a complete run ("status running, N=2"), or None."""
    run_meta = run_meta or {}
    status = run_meta.get("status", (run_meta.get("run") or {}).get("status"))
    partial = bool(run_meta.get("cancelled")) or bool((run_meta.get("subset") or {}).get("partial"))
    if not partial and status in (None, "done"):
        return None
    # A run.json still "running" (stopped or in progress) holds the requested N; snapshot_n is what was processed
    n = run_meta.get("snapshot_n") if run_meta.get("status") == "running" else _run_n(run_meta)
    state = "partial" if partial else f"status {status}"
    return f"{state}, N={NOT_RECORDED if n is None else n}"


def _run_n(run_meta):
    """N of a run record (run.json "n" or "subjects.n") or of a provenance.json ("N")."""
    run_meta = run_meta or {}
    if "N" in run_meta:
        return run_meta["N"]
    return run_meta.get("n", (run_meta.get("subjects") or {}).get("n"))


def _subject_list(run_meta, partial=False):
    """{"name", "source", "file", "sha256"} of a run's --subject-list (values None when not used).

    :param partial: the run was stopped and used only the first subjects of its list: "sha256" is None
      and the list's hash is moved to "requested_sha256" (it does not identify the subjects used).
    """
    run_meta = run_meta or {}
    if isinstance(run_meta.get("subject_list"), dict):
        out = dict(run_meta["subject_list"])
    else:
        subjects = run_meta.get("subjects") or {}
        out = {"name": subjects.get("list_name"), "source": subjects.get("list_source"), "file": subjects.get("list_file"), "sha256": subjects.get("list_sha256")}
    if partial and out.get("sha256"):
        out.update({"sha256": None, "requested_sha256": out["sha256"]})
    return out


# Export


def _resolve_rule(rule, resolved, n, n_voxels, note):
    from . import thresholds

    rule = thresholds.normalize_rule(rule)
    if resolved is None:
        resolved = thresholds.resolve(rule, n, n_voxels, note=note)
    elif isinstance(resolved, dict):
        resolved = thresholds.Resolved.from_dict(resolved)
    return rule, resolved, thresholds.describe(resolved)


def _software(run_meta, software):
    out = {"name": "3D Slicer (Slicer Studio fork) lesion network mapping", "slicer": None, "fork_revision": None, LIBRARY_NAME: LIBRARY_VERSION}
    out.update((run_meta or {}).get("software") or {})
    out.update(software or {})
    return out


def write_run(out_dir, maps, lesion_names, bundle, rule=None, resolved=None, qa=None, run_meta=None, preview_n=None, software=None):
    """Export a network-mapping run: maps, one sidecar per lesion, provenance.json, methods.md and reproduce.sh.

    :param out_dir: destination folder (created if needed).
    :param maps: ``engine.NetworkMaps`` (the maps are written), or {lesion name: {"AvgR", "AvgFz", "T": path}}
      of existing map files (copied unchanged, so the exact connectome header is kept).
    :param lesion_names: lesion names, in map order.
    :param bundle: ``ConnectomeBundle``, or a bundle info dict (``bundle_info()`` or a run record's "bundle").
    :param rule: threshold rule (dict or JSON; default FWE). Maps are written unthresholded; the rule and
      its resolved value are recorded.
    :param resolved: ``thresholds.Resolved`` (or its dict); default: resolved from the rule, N and V.
    :param qa: {lesion name: QA values} as in the engine's "done" event or qa.json "lesions".
    :param run_meta: the engine's run record (``--run-json``), used for the subject hash, seeds,
      lesion files, timings and the command line; may be None.
    :param preview_n: number of subjects of a preview (subset) run: adds "PreviewN<k>" to the desc labels.
    :param software: optional {"slicer": version, "fork_revision": revision} of the exporting application.
    :return: {"maps": [paths], "sidecars": [paths], "provenance": path, "methods": path, "reproduce": path}.
    """
    from .engine import OUTPUT_KEYS, write_maps

    run_meta = dict(run_meta or {})
    os.makedirs(out_dir, exist_ok=True)
    info = bundle_info(bundle)
    lesion_names = list(lesion_names)
    if preview_n is None:
        preview_n = _preview_from_suffix(run_meta.get("desc_suffix"))
    suffix = preview_suffix(preview_n)
    if not suffix and _subject_list(run_meta)["name"]:
        suffix = run_meta.get("desc_suffix") or ""  # a named-subset run keeps its "Subset<Name>" label

    # Maps
    written_maps = {}
    if isinstance(maps, dict):
        n = int(run_meta.get("n", (run_meta.get("subjects") or {}).get("n", 0)))
        cancelled = bool(run_meta.get("cancelled", False))
        subject_hash = run_meta.get("subject_ids_sha256")
        for name in lesion_names:
            targets = map_paths(out_dir, name, info["name"], desc_suffix=suffix)
            written_maps[name] = {}
            for key, source in maps[name].items():
                if not os.path.isfile(source):
                    raise FileNotFoundError(f"Map file not found: {source}")
                if not (os.path.exists(targets[key]) and os.path.samefile(source, targets[key])):
                    shutil.copyfile(source, targets[key])
                written_maps[name][key] = targets[key]
    else:
        if isinstance(bundle, dict):
            raise ValueError("Writing maps from memory needs the ConnectomeBundle")
        n, cancelled, subject_hash = maps.n, maps.cancelled, subjects_sha256(maps.subject_ids)
        paths = write_maps(maps, lesion_names, bundle, out_dir, suffix)
        for index, name in enumerate(lesion_names):
            written_maps[name] = dict(zip((key for key, _ in OUTPUT_KEYS), paths[3 * index:3 * index + 3], strict=True))
    note = "partial" if cancelled else "preview" if preview_n else None
    rule, resolved, derived = _resolve_rule(rule if rule is not None else run_meta.get("threshold_rule"), resolved, n, info["n_voxels"], note)
    subset = {
        "preview": bool(preview_n),
        "partial": cancelled,
        "n_requested": (run_meta.get("subjects") or {}).get("requested", preview_n),
        "random": bool((run_meta.get("subjects") or {}).get("random", False)),
        "seed": run_meta.get("seed", (run_meta.get("subjects") or {}).get("seed", 0)),
    }
    threshold = {"rule": rule, "resolved": resolved.to_dict(), "derived": derived}
    lesion_meta = {entry.get("name"): entry for entry in run_meta.get("lesions", []) if isinstance(entry, dict)}
    timestamp = now_iso()
    software = _software(run_meta, software)
    subject_list = _subject_list(run_meta, partial=cancelled)
    connectome = {key: info.get(key) for key in ("name", "label", "format_version", "manifest_sha256", "citation", "space", "n_subjects", "n_voxels", "voxel_size_mm")}
    # Connectome identity (checklist item B4): DOI, version, subject list, GSR and mask, None when not recorded
    connectome.update({key: info.get(key) for key in ("doi", "dataset_version", "licence", "terms_sha256", "acknowledgement", "gsr", "mask_sha256")})
    connectome.update({"subset": subject_list["name"], "subject_list_sha256": subject_list["sha256"]})
    licence = info.get("licence")
    acknowledgement = info.get("acknowledgement") or (GSP_ACKNOWLEDGEMENT if licence == GSP_LICENCE else None)
    connectome["acknowledgement"] = acknowledgement

    # Sidecars
    sidecars, lesions = [], []
    for name in lesion_names:
        meta = lesion_meta.get(name, {})
        lesion = {
            "name": name,
            "file": meta.get("file"),
            "sha256": meta.get("sha256"),
            "voxels_on_connectome_grid": meta.get("voxels_in_mask"),
            "fractional_sum": meta.get("weight_total"),
            "volume_mm3": meta.get("volume_mm3"),
            "outside_connectome_fraction": meta.get("weight_outside_fraction"),
        }
        sidecar = {
            "format": SIDECAR_FORMAT,
            "version": 1,
            "Description": "Lesion network map: seed-based functional connectivity of the lesion with a normative connectome",
            "software": software,
            "connectome": connectome,
            "lesion": lesion,
            "N": n,
            "df": max(n - 1, 0),
            "subject_ids_sha256": subject_hash,
            "subset": subset,
            "threshold": threshold,
            "qa": _clean((qa or {}).get(name)),
            "maps": {key: os.path.basename(path) for key, path in written_maps[name].items()},
            "timestamp": timestamp,
            "disclaimer": DISCLAIMER,
        }
        if licence:
            sidecar["terms"] = licence  # derived maps carry the data use terms of their connectome (e.g. GSP-DUT-2014)
        path = sidecar_path(out_dir, name, info["name"], desc_suffix=suffix)
        write_json_atomic(path, _clean(sidecar))
        sidecars.append(path)
        lesions.append({**lesion, "maps": sidecar["maps"], "sidecar": os.path.basename(path), "qa": sidecar["qa"]})

    # Run level
    provenance_path = os.path.join(out_dir, "provenance.json")
    methods_path = os.path.join(out_dir, "methods.md")
    reproduce_path = os.path.join(out_dir, "reproduce.sh")
    provenance = {
        "format": PROVENANCE_FORMAT,
        "version": 1,
        "created": timestamp,
        "software": software,
        "versions": run_meta.get("versions") or versions(),
        "host": run_meta.get("host") or host_info(),
        "connectome": info,
        "N": n,
        "df": max(n - 1, 0),
        "subject_ids_sha256": subject_hash,
        "subject_list": subject_list,
        "subset": subset,
        "threshold": threshold,
        "null": run_meta.get("null"),
        "lesions": lesions,
        "run": {key: run_meta.get(key) for key in ("argv", "cwd", "threads", "start", "end", "elapsed", "engine_elapsed", "status", "desc_suffix") if key in run_meta},
        "files": [os.path.basename(p) for name in lesion_names for p in written_maps[name].values()] + [os.path.basename(p) for p in sidecars] + ["methods.md", "reproduce.sh"],
        "citations": [CITATIONS["boes2015"], CITATIONS["fox2018"], *([info["citation"]] if info.get("citation") else []), *(CITATIONS[key] for key in DEBATE_CITATIONS)],
        "terms": licence,
        "acknowledgement": acknowledgement,
        "disclaimer": DISCLAIMER,
    }
    write_json_atomic(provenance_path, _clean(provenance))
    _write_text_atomic(methods_path, methods_text(info, n, subset, threshold, lesions, run_meta))
    _write_text_atomic(reproduce_path, reproduce_script(run_meta, info, lesions, preview_n), mode=0o755)
    return {
        "maps": [p for name in lesion_names for p in written_maps[name].values()],
        "sidecars": sidecars,
        "provenance": provenance_path,
        "methods": methods_path,
        "reproduce": reproduce_path,
    }


def export_run(run_dir, out_dir, rule=None, run_json=None, software=None, partial=False):
    """Export an engine run (a folder written with ``--run-json``) with :func:`write_run`.

    :param run_dir: the engine's ``--out`` folder (holds the maps and qa.json).
    :param run_json: the run record; default ``<run_dir>/run.json``.
    :param rule: threshold rule; default: the run's ``--threshold-rule``, else FWE.
    :param partial: also export a stopped run (terminated after writing snapshots): its latest
      snapshot maps are exported as a partial run with N = the snapshot's subject count.
    """
    run_json = run_json or os.path.join(run_dir, "run.json")
    if not os.path.isfile(run_json):
        raise FileNotFoundError(f"Run record not found: {run_json} (run the engine with --run-json)")
    with open(run_json, encoding="utf-8") as f:
        run_meta = json.load(f)
    if run_meta.get("status") != "done":
        if not (partial and run_meta.get("status") == "running" and run_meta.get("snapshot_n") and run_meta.get("maps")):
            raise ValueError(f"The run in {run_dir} did not finish (status {run_meta.get('status')!r})")
        run_meta.update({"n": run_meta["snapshot_n"], "cancelled": True, "subject_ids_sha256": None})
    qa = {}
    qa_path = run_meta.get("qa_json") or os.path.join(run_dir, "qa.json")
    if run_meta.get("status") == "done" and os.path.isfile(qa_path):
        with open(qa_path, encoding="utf-8") as f:
            qa = json.load(f).get("lesions") or {}
    names = [entry["name"] for entry in run_meta["lesions"]]
    missing = [name for name in names if name not in qa]
    if missing:
        qa.update(_degree_qa_from_files(run_meta, missing))
    return write_run(out_dir, run_meta["maps"], names, run_meta["bundle"], rule=rule, qa=qa, run_meta=run_meta, software=software)


def _degree_qa_from_files(run_meta, names):
    """Degree similarity of the T map files of some lesions (when the connectome and its degree map are available)."""
    from . import qa as qa_lib
    from .connectome import ConnectomeBundle
    from .nifti import read_nifti

    try:
        bundle = ConnectomeBundle.open(run_meta["bundle"]["path"])
        degree = bundle.degree
    except (KeyError, OSError, ValueError):
        return {}
    out = {}
    for name in names:
        t_map = read_nifti(run_meta["maps"][name]["T"])[0]
        out[name] = qa_lib.lesion_qa(t_map.reshape(-1)[bundle.mask_indices], degree)
    return out


def _format_number(value, digits=2):
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}"


def methods_text(info, n, subset, threshold, lesions, run_meta=None):
    """Generated methods.md: a paragraph, the parameters, QA values, citations and the disclaimer."""
    run_meta = run_meta or {}
    null = run_meta.get("null") or {}
    resolved = threshold["resolved"]
    spacing = info.get("voxel_size_mm") or []
    spacing_text = "x".join(f"{v:g}" for v in spacing) + " mm" if spacing else "connectome"
    subject_list = _subject_list(run_meta, partial=subset["partial"])
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
    if resolved["statistic"] == "Fz":
        threshold_text = f"Maps were displayed at |mean Fz| ≥ {resolved['value']:g} (effect size; maps are stored unthresholded)."
    else:
        p_text = f", two-sided p_unc ≤ {resolved['p_unc']:.2g}" if resolved.get("p_unc") is not None else ""
        threshold_text = f"T maps were thresholded at |T| ≥ {_format_number(resolved['value'])} (rule {threshold['rule']['kind']}, df = {resolved['df']}, V = {resolved['v']:,} voxels{p_text}); maps are stored unthresholded."
    qa_text = "Specificity QA followed van den Heuvel et al. (2026): the spatial correlation between each T map and the connectome degree map was computed when the degree map was available"
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
        f"Lesion network maps were computed with the lesion network mapping library of 3D Slicer ({LIBRARY_NAME} {LIBRARY_VERSION}). "
        f"Each lesion was resampled onto the {spacing_text} grid of the {info.get('name')} normative functional connectome ({info.get('n_voxels', 0):,} voxels{identity_text}) as fractional voxel occupancy. "
        f"For each of N = {n} subjects{subset_text}, the occupancy-weighted time course of the lesion was correlated (Pearson r) with the time course of every voxel, "
        "the correlations were averaged over runs and Fisher z-transformed, and maps of the mean r, the mean Fisher z and the voxelwise one-sample t statistic of Fisher z "
        f"across subjects (df = {max(n - 1, 0)}) were computed (Boes et al., 2015; Fox, 2018). {threshold_text} {qa_text}"
    )
    lines = [
        "# Methods",
        "",
        paragraph,
        "",
        "## Parameters",
        "",
        "| Parameter | Value |",
        "|---|---|",
        f"| Connectome | {info.get('name')} (manifest sha256 {info.get('manifest_sha256') or 'n/a'}) |",
        f"| Connectome DOI and version | {('doi:' + info['doi']) if info.get('doi') else NOT_RECORDED}; version {info.get('dataset_version') or NOT_RECORDED} |",
        f"| Global signal regression | {'yes' if info.get('gsr') is True else 'no' if info.get('gsr') is False else NOT_RECORDED} |",
        f"| Connectome mask sha256 | {info.get('mask_sha256') or NOT_RECORDED} |",
        f"| Data use terms | {info.get('licence') or NOT_RECORDED} |",
        f"| Subjects (N) | {n} of {info.get('n_subjects')} |",
        f"| Subset | {'preview' if subset['preview'] else 'partial' if subset['partial'] else 'subject list' if subject_list['sha256'] else 'all'}; random {str(subset['random']).lower()}; seed {subset['seed']} |",
        f"| Subject list | {_subject_list_text(subject_list)} |",
        f"| Threshold rule | `{json.dumps(threshold['rule'], sort_keys=True)}` |",
        f"| Resolved threshold | {threshold['derived']} |",
        f"| Relocated-lesion null | {null.get('k', 0)} per lesion, seed {null.get('seed', 0)} |",
        "",
        "## Lesions and QA",
        "",
        "| Lesion | Voxels on connectome grid | Fractional sum | Degree r | Degree rho | Null r (median) | Null r (mean map) | Null percentile |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for lesion in lesions:
        qa = lesion.get("qa") or {}
        null_summary = qa.get("null") or {}
        lines.append(
            f"| {lesion['name']} | {lesion.get('voxels_on_connectome_grid') if lesion.get('voxels_on_connectome_grid') is not None else 'n/a'} | {_format_number(lesion.get('fractional_sum'), 1)} | "
            f"{_format_number(qa.get('degree_r'))} | {_format_number(qa.get('degree_rho'))} | {_format_number(null_summary.get('r_median'))} | "
            f"{_format_number(null_summary.get('r_mean_map'))} | {_format_number(null_summary.get('percentile'), 0)} |",
        )
    lines += ["", "## Citations", ""]
    lines += [f"- {CITATIONS['boes2015']}", f"- {CITATIONS['fox2018']}"]
    if info.get("citation"):
        lines.append(f"- Connectome: {info['citation']}")
    lines += [f"- {CITATIONS[key]}" for key in DEBATE_CITATIONS]
    acknowledgement = info.get("acknowledgement") or (GSP_ACKNOWLEDGEMENT if info.get("licence") == GSP_LICENCE else None)
    if acknowledgement:
        lines += ["", "## Acknowledgements", "", acknowledgement]
    lines += ["", "## Disclaimer", "", DISCLAIMER, ""]
    return "\n".join(lines)


def _subject_list_text(subject_list):
    name = f"subset \"{subject_list['name']}\"" if subject_list.get("name") else f"from {subject_list.get('source')}"
    if subject_list.get("requested_sha256"):
        return f"{name}; requested list sha256 {subject_list['requested_sha256']} (partial run: only its first subjects were used)"
    if not subject_list.get("sha256"):
        return "not used"
    return f"{name}; sha256 {subject_list['sha256']}"


def _absolute_option_values(argv, cwd):
    out = []
    expect_path = False
    for token in argv:
        if expect_path:
            out.append(token if os.path.isabs(token) or not cwd else os.path.normpath(os.path.join(cwd, token)))
            expect_path = False
            continue
        option, has_value, value = token.partition("=")
        if option in _PATH_OPTIONS:
            if has_value:
                out.append(f"{option}={value if os.path.isabs(value) or not cwd else os.path.normpath(os.path.join(cwd, value))}")
            else:
                out.append(token)
                expect_path = True
            continue
        out.append(token)
    return out


def _replace_option_value(argv, option, value):
    """``argv`` with the value of ``option`` ("--opt value" or "--opt=value") replaced."""
    out, replace_next = [], False
    for token in argv:
        if replace_next:
            out.append(value)
            replace_next = False
        elif token == option:
            out.append(token)
            replace_next = True
        elif token.startswith(option + "="):
            out.append(f"{option}={value}")
        else:
            out.append(token)
    return out


def _engine_arguments(run_meta, info, lesions, preview_n):
    """Engine arguments of a run, without --out and --run-json (added by reproduce.sh)."""
    argv = run_meta.get("argv")
    subject_list = _subject_list(run_meta)
    if argv:
        argv = _absolute_option_values(argv, run_meta.get("cwd"))
        if subject_list["source"] == "file" and subject_list["file"]:
            argv = _replace_option_value(argv, "--subject-list", subject_list["file"])
        out, skip = [], False
        for token in argv:
            if skip:
                skip = False
                continue
            option = token.partition("=")[0]
            if option in ("--out", "--run-json"):
                skip = "=" not in token
                continue
            out.append(token)
        return out
    out = ["--bundle", info.get("path") or info.get("name")]
    for lesion in lesions:
        out += ["--lesion", lesion.get("file") or f"{lesion['name']}.nii.gz"]
    subjects = run_meta.get("subjects") or {}
    if subject_list["name"]:
        out += ["--subject-list", subject_list["name"]]  # the engine refuses --subjects/--random with it
        if preview_n:
            out += ["--desc-suffix", preview_suffix(preview_n)]
    elif preview_n:
        out += ["--subjects", str(preview_n), "--desc-suffix", preview_suffix(preview_n)]
        if subjects.get("random"):
            out += ["--random", "--seed", str(subjects.get("seed", 0))]
    null = run_meta.get("null") or {}
    if null.get("k"):
        out += ["--null-relocations", str(null["k"]), "--null-seed", str(null.get("seed", 0))]
    return out


def reproduce_script(run_meta, info, lesions, preview_n=None):
    """reproduce.sh: the PythonSlicer command line that recomputes the run's maps into $OUT."""
    from .connectome import MANIFEST_NAME

    library_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    arguments = _engine_arguments(run_meta or {}, info, lesions, preview_n)
    lines = [
        "#!/bin/sh",
        "# Recompute this lesion network mapping run (generated by 3D Slicer's lesion network mapping library).",
        "# Usage: [PYTHONSLICER=/path/to/Slicer/bin/PythonSlicer] [OUT=output_folder] sh reproduce.sh",
        f"# The connectome bundle and the lesion files must be available at the paths below; their sha256 are in provenance.json ({MANIFEST_NAME} sha256 {info.get('manifest_sha256') or 'n/a'}).",
        f"# {DISCLAIMER}",
        "set -e",
        'PYTHONSLICER="${PYTHONSLICER:-PythonSlicer}"',
        'OUT="${OUT:-reproduced}"',
        f'LNM_LIBRARY_DIR="${{LNM_LIBRARY_DIR:-{library_dir}}}"',
        'PYTHONPATH="$LNM_LIBRARY_DIR${PYTHONPATH:+:$PYTHONPATH}"',
        "export PYTHONPATH",
        '"$PYTHONSLICER" -m LesionNetworkMappingLib.engine \\',
    ]
    if (run_meta or {}).get("cancelled"):
        lines.insert(5, f"# The run was stopped after {run_meta.get('n')} subjects: this command recomputes the complete run.")
    lines += [f"  {shlex.quote(token)} \\" for token in arguments]
    lines += ['  --out "$OUT" --run-json "$OUT/run.json"', ""]
    return "\n".join(lines)

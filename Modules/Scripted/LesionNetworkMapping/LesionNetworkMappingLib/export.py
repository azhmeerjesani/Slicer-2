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
(:data:`DISCLAIMER`), N, df, the subset/preview flag and seed, the threshold rule and its resolved value.
All files are plain NIfTI-1, JSON, Markdown and shell text.
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

DISCLAIMER = (
    "Research use only — not for clinical decisions. Lesion network mapping specificity is debated "
    "(van den Heuvel et al., 2026, Nat Neurosci, doi:10.1038/s41593-025-02196-7)."
)
CITATIONS = {
    "boes2015": "Boes AD, Prasad S, Liu H, et al. Network localization of neurological symptoms from focal brain lesions. Brain. 2015;138(10):3061-3075. doi:10.1093/brain/awv228",
    "fox2018": "Fox MD. Mapping symptoms to brain networks with the human connectome. N Engl J Med. 2018;379(23):2237-2245. doi:10.1056/NEJMra1706158",
    "vandenheuvel2026": "van den Heuvel et al. Nat Neurosci. 2026. doi:10.1038/s41593-025-02196-7",
}
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


def map_paths(out_dir, lesion_name, bundle_name, preview_n=None):
    """Map file names of one lesion: {"AvgR", "AvgFz", "T": path} (see ``engine.output_paths``)."""
    from .engine import output_paths

    return output_paths(out_dir, lesion_name, bundle_name, preview_suffix(preview_n))


def sidecar_path(out_dir, lesion_name, bundle_name, preview_n=None):
    """Sidecar file name of one lesion: <lesion>_conn-<label>[_desc-PreviewN<k>]_lnm.json."""
    desc = f"_desc-{preview_suffix(preview_n)}" if preview_n else ""
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


def bundle_info(bundle):
    """JSON-compatible description of a connectome bundle (also accepts an existing info dict)."""
    if isinstance(bundle, dict):
        return dict(bundle)
    manifest = os.path.join(bundle.path, "connectome.json")
    metadata = getattr(bundle, "metadata", {})
    return {
        "name": bundle.name,
        "label": connectome_label(bundle.name),
        "path": bundle.path,
        "format_version": metadata.get("version"),
        "manifest_sha256": file_sha256(manifest) if os.path.isfile(manifest) else None,
        "description": bundle.description,
        "citation": bundle.citation,
        "space": metadata.get("space"),
        "n_subjects": bundle.n_subjects,
        "n_voxels": bundle.n_voxels,
        "shape": list(bundle.shape),
        "affine": _clean(bundle.affine),
        "voxel_size_mm": [round(float(v), 6) for v in bundle.grid.spacing],
    }


def run_record(argv, bundle, subject_ids, args=None, rule=None):
    """Initial run record written by the engine's --run-json (status "running"; completed at the end)."""
    record = {
        "format": RUN_FORMAT,
        "version": 1,
        "status": "running",
        "argv": list(argv),
        "cwd": os.getcwd(),
        "bundle": bundle_info(bundle),
        "subjects": {
            "n": len(subject_ids),
            "requested": getattr(args, "subjects", None),
            "random": bool(getattr(args, "random", False)),
            "seed": getattr(args, "seed", 0),
        },
        "subject_ids_sha256": subjects_sha256(subject_ids),
        "seed": getattr(args, "seed", 0),
        "desc_suffix": getattr(args, "desc_suffix", ""),
        "threshold_rule": rule,
        "threads": getattr(args, "threads", None),
        "versions": versions(),
        "host": host_info(),
        "start": now_iso(),
        "end": None,
    }
    return record


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

    # Maps
    written_maps = {}
    if isinstance(maps, dict):
        n = int(run_meta.get("n", (run_meta.get("subjects") or {}).get("n", 0)))
        cancelled = bool(run_meta.get("cancelled", False))
        subject_hash = run_meta.get("subject_ids_sha256")
        for name in lesion_names:
            targets = map_paths(out_dir, name, info["name"], preview_n)
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
    connectome = {key: info.get(key) for key in ("name", "label", "format_version", "manifest_sha256", "citation", "space", "n_subjects", "n_voxels", "voxel_size_mm")}

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
        path = sidecar_path(out_dir, name, info["name"], preview_n)
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
        "subset": subset,
        "threshold": threshold,
        "null": run_meta.get("null"),
        "lesions": lesions,
        "run": {key: run_meta.get(key) for key in ("argv", "cwd", "threads", "start", "end", "elapsed", "engine_elapsed", "status", "desc_suffix") if key in run_meta},
        "files": [os.path.basename(p) for name in lesion_names for p in written_maps[name].values()] + [os.path.basename(p) for p in sidecars] + ["methods.md", "reproduce.sh"],
        "citations": [CITATIONS["boes2015"], CITATIONS["fox2018"], *([info["citation"]] if info.get("citation") else []), CITATIONS["vandenheuvel2026"]],
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
    if subset["preview"]:
        subset_text = f" (preview: a {'random ' if subset['random'] else ''}subset of {n} of {info.get('n_subjects')} subjects{', seed ' + str(subset['seed']) if subset['random'] else ''})"
    elif subset["partial"]:
        subset_text = f" (partial run: {n} of {info.get('n_subjects')} subjects)"
    else:
        subset_text = ""
    if resolved["statistic"] == "Fz":
        threshold_text = f"Maps were displayed at |mean Fz| ≥ {resolved['value']:g} (effect size; maps are stored unthresholded)."
    else:
        p_text = f", two-sided p_unc ≤ {resolved['p_unc']:.2g}" if resolved.get("p_unc") is not None else ""
        threshold_text = f"T maps were thresholded at |T| ≥ {_format_number(resolved['value'])} (rule {threshold['rule']['kind']}, df = {resolved['df']}, V = {resolved['v']:,} voxels{p_text}); maps are stored unthresholded."
    qa_text = "Specificity QA followed van den Heuvel et al. (2026): the spatial correlation between each T map and the connectome degree map was computed when the degree map was available"
    if null.get("k"):
        qa_text += (
            f", and each lesion was rigidly relocated to {null['k']} random positions inside the brain mask "
            f"(integer voxel shifts on the connectome grid, ≥ {int(100 * null.get('min_inside', 0.95))}% of the lesion weight inside, seed {null.get('seed', 0)}) "
            "and mapped in the same pass; the similarity between each map and the maps of its relocated lesions is reported"
        )
    qa_text += "."
    paragraph = (
        f"Lesion network maps were computed with the lesion network mapping library of 3D Slicer ({LIBRARY_NAME} {LIBRARY_VERSION}). "
        f"Each lesion was resampled onto the {spacing_text} grid of the {info.get('name')} normative functional connectome ({info.get('n_voxels', 0):,} voxels) as fractional voxel occupancy. "
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
        f"| Subjects (N) | {n} of {info.get('n_subjects')} |",
        f"| Subset | {'preview' if subset['preview'] else 'partial' if subset['partial'] else 'all'}; random {str(subset['random']).lower()}; seed {subset['seed']} |",
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
    lines += [f"- {CITATIONS['vandenheuvel2026']}", "", "## Disclaimer", "", DISCLAIMER, ""]
    return "\n".join(lines)


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


def _engine_arguments(run_meta, info, lesions, preview_n):
    """Engine arguments of a run, without --out and --run-json (added by reproduce.sh)."""
    argv = run_meta.get("argv")
    if argv:
        argv = _absolute_option_values(argv, run_meta.get("cwd"))
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
    if preview_n:
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

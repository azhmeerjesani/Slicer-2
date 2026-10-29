"""slicer-lnm: the headless twin of Slicer Studio's lesion network mapping, as a BIDS-App command line.

The same pure numpy/scipy library runs in the application (Map, Library ▸ Analysis) and on a server or
an HPC cluster (the ``slicer-lnm`` wheel, the container image of ``Utilities/SlicerStudioLNM`` or
``Slicer Studio.app/Contents/bin/slicer-lnm``), so that a GUI run and a batch run of the same study,
connectome and settings give the same files, within numerical tolerance (DE-13)::

    slicer-lnm <input_dir> <output_dir> {participant,group} --connectome PATH --accept-terms
        [--participant-label L [L ...]] [--rule JSON|FILE] [--recipe NAME] [--design design.json]
        [--null-relocations K] [--perm N] [--figures PRESET[,PRESET...]] [--threads K] [...; see --help]
    slicer-lnm <command> [arguments]   a library job: slicer-lnm engine ..., slicer-lnm pipeline run ..., ...
    slicer-lnm --version

**Input.** A BIDS dataset whose lesion masks are named ``*_label-lesion_mask``, ``*_desc-lesion_mask``
or ``*_label-lesion_roi`` (``.nii`` or ``.nii.gz``, raw data or derivatives), or a Slicer Studio study
folder (study.json, participants.tsv). For a BIDS dataset a study is written (or updated) in
``<output_dir>/sourcedata/slicer-lnm-study``: it references the lesion files in place (relative paths;
nothing in the input folder is written) and imports the columns of the dataset's participants.tsv
(``--group-column``/``--group-map`` map the group values to case and control, ``--score-columns``
confirms score columns for sLNM). The folder opens in the Library of Slicer Studio. A study folder is
used as it is, like the application does (the pipeline posts to its inbox and keeps its map cache).

**Levels.** Both levels run :func:`pipeline.run` (A-04) on the study, writing into
``<output_dir>/conn-<label>[_n-<N>]``, the folder name of the application's
``<study>/derivatives/lnm/conn-<label>[_n-<N>]``:

- ``participant``: the stages up to the network maps (preflight, normalization, reference maps, the
  batched cohort maps with the per-lesion relocation nulls), the per-lesion QA and the per-lesion export
  (maps, sidecars, provenance.json, methods, reproduce.sh). The cohort stages are recorded in
  pipeline.json as "deferred to the group level".
- ``group``: every stage. The stages that the participant level completed are reused (the maps come
  from the per-lesion cache, nothing is recomputed); overlap, nulls, lesion-set diagnostics, the GLM
  (``--design``), sLNM, conjunction, QA, clusters, export, methods, checklist and report follow.

``--participant-label`` restricts either level to those subjects (the others are listed in pipeline.json
as "not selected"). The connectome is read in one streaming pass per batch of up to 100 lesions, so one
participant-level job with ``--threads K`` maps a cohort faster than one job per subject; jobs that
write to the same output folder run one after the other (``--lock-wait``).

``<output_dir>/dataset_description.json`` is the one the application writes in ``<study>/derivatives/lnm``
(DatasetType "derivative"). Exit codes are those of the pipeline: 0 done (also with failed optional
stages), 1 error, 2 usage, 3 cancelled, 4 data use terms not accepted, 5 blocked by preflight,
6 no eligible lesion, 75 output folder locked.

**Library jobs.** ``slicer-lnm <command> ...`` runs ``python -m LesionNetworkMappingLib.<command> ...``
(every library module with a command line: engine, cohortmaps, pipeline, glm, slnm, nulls, qa, figures,
recipes, ...). reproduce.sh uses ``slicer-lnm engine`` when ``slicer-lnm`` is on PATH. To analyse an input
folder that has the name of a command, write it as ``./<name>``.

**Wheel version.** :data:`__version__` (read by pyproject.toml when the wheel is built) is
``export.LIBRARY_VERSION + "+fork.<rev>"``, where ``<rev>`` is ``$SLICER_FORK_REV``, else the git
revision of the source folder, else "local". This module imports only the standard library until a
command runs (the BLAS thread count is set before numpy is imported).
"""

import os
import sys

if not __package__:  # run as a script (python <Lib>/app.py ...)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "LesionNetworkMappingLib"  # noqa: A001

import functools
import json
import re
import runpy
import shutil
import subprocess
import tempfile
import time

PACKAGE = __package__ or "LesionNetworkMappingLib"
LIB_DIR = os.path.dirname(os.path.abspath(__file__))
DISTRIBUTION = "slicer-lnm"
PROG = "slicer-lnm"
LEVELS = ("participant", "group")
#: Stages of the participant level; the other stages of pipeline.STAGES run at the group level only
PARTICIPANT_STAGES = ("prepare", "preflight", "normalize", "reference", "maps", "qa", "export", "finish")
DEFERRED = "deferred to the group level (slicer-lnm ... group)"
NOT_SELECTED = "not selected (--participant-label)"
#: Study written for a BIDS input, relative to the output folder
STUDY_RELPATH = ("sourcedata", "slicer-lnm-study")
STUDY_NAME = "slicer-lnm BIDS input"
LESION_RULE = "lesion"
FIGURE_PRESETS = ("montage", "pair", "glass", "ortho", "gallery")
FIGURE_MAP_DESC = "meanT"
LOCK_POLL_S = 5.0
_ENTITY = re.compile(r"^([a-zA-Z0-9]+)-([a-zA-Z0-9]+)$")
_NIFTI = re.compile(r"\.nii(?:\.gz)?$", re.IGNORECASE)
#: BIDS space label of a user template ("user:<id>" -> "user<Id>", normalize.template_label)
_USER_SPACE = re.compile(r"^user[A-Z0-9][A-Za-z0-9]*$")


# Version


def _git_revision(folder):
    try:
        result = subprocess.run(["git", "-C", folder, "rev-parse", "--short=12", "HEAD"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def build_version(revision=None):
    """``LIBRARY_VERSION+fork.<rev>`` (a PEP 440 local version): ``revision``, ``$SLICER_FORK_REV``, the git revision, or "local"."""
    try:
        from .export import LIBRARY_VERSION
    except ImportError:  # loaded alone by setuptools (pyproject.toml "attr"), without its package
        with open(os.path.join(LIB_DIR, "export.py"), encoding="utf-8") as f:
            LIBRARY_VERSION = re.search(r'^LIBRARY_VERSION = "([^"]+)"', f.read(), re.MULTILINE).group(1)  # noqa: N806

    rev = revision or os.environ.get("SLICER_FORK_REV") or _git_revision(LIB_DIR) or "local"
    rev = re.sub(r"[^A-Za-z0-9.]+", ".", str(rev)).strip(".").lower() or "local"
    return f"{LIBRARY_VERSION}+fork.{rev}"


@functools.lru_cache(maxsize=1)
def version():
    """Version of the running library: the installed slicer-lnm distribution when this file belongs to it, else :func:`build_version`."""
    try:
        from importlib import metadata

        dist = metadata.distribution(DISTRIBUTION)
        if os.path.realpath(str(dist.locate_file(os.path.join(PACKAGE, "app.py")))) == os.path.realpath(__file__):
            return dist.version
    except Exception:  # not installed as a distribution (application, source tree)
        pass
    return build_version()


def __getattr__(name):
    if name == "__version__":  # computed on access: pyproject.toml reads it when the wheel is built
        return build_version()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Library jobs


@functools.lru_cache(maxsize=1)
def library_commands():
    """Names of the library modules with a command line (``python -m LesionNetworkMappingLib.<name>``)."""
    names = []
    for file_name in sorted(os.listdir(LIB_DIR)):
        name, extension = os.path.splitext(file_name)
        if extension != ".py" or name.startswith("_") or name == "app":
            continue
        try:
            with open(os.path.join(LIB_DIR, file_name), encoding="utf-8") as f:
                if 'if __name__ == "__main__"' in f.read():
                    names.append(name)
        except OSError:
            continue
    return tuple(names)


def run_library_command(name, argv):
    """Run ``python -m LesionNetworkMappingLib.<name> <argv>`` in this process; return its exit code."""
    saved = sys.argv
    sys.argv = [f"{PROG} {name}", *argv]
    try:
        runpy.run_module(f"{PACKAGE}.{name}", run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        sys.argv = saved
    return 0


# BIDS input


def bids_entities(path):
    """(entities {key: value}, suffix) of a BIDS file name, or None when the name is not BIDS-like."""
    name = _NIFTI.sub("", os.path.basename(path))
    parts = name.split("_")
    if len(parts) < 2:
        return None
    entities = {}
    for part in parts[:-1]:
        match = _ENTITY.match(part)
        if not match:
            return None
        entities[match.group(1)] = match.group(2)
    return entities, parts[-1]


def is_lesion_file(path):
    """Whether ``path`` is a BIDS lesion mask: ``*_label-lesion_mask``, ``*_label-lesion_roi`` or ``*_desc-lesion_mask`` (.nii or .nii.gz)."""
    if not _NIFTI.search(os.path.basename(path)):
        return False
    parsed = bids_entities(path)
    if parsed is None or "sub" not in parsed[0]:
        return False
    entities, suffix = parsed
    return (entities.get("label") == "lesion" and suffix in ("mask", "roi")) or (entities.get("desc") == "lesion" and suffix == "mask")


def _choice_key(path):
    entities = bids_entities(path)[0]
    space = entities.get("space", "")
    # template-space masks first (they need no normalization), then the shortest path (raw data before derivatives)
    return (not (space.upper().startswith("MNI") or _USER_SPACE.match(space)), path.count(os.sep), path)


def _sidecar_space(path):
    """The template space that the JSON sidecar of a BIDS file names (``SlicerStudio.space``, then ``SpatialReference``,
    as normalize and Study.write_lesion_sidecar write them), or None when it names no template of a known family.
    """
    from .geometry import space_family

    try:
        with open(_NIFTI.sub("", path) + ".json", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    studio = data.get("SlicerStudio")
    for value in ((studio or {}).get("space") if isinstance(studio, dict) else None, data.get("SpatialReference")):
        if isinstance(value, str) and space_family(value) not in ("native", "unknown"):
            return value
    return None


def bids_lesion_space(path):
    """The template space of a BIDS lesion mask, or None when its grid decides (no space entity, or a subject space).

    A ``space`` entity of a known template family (MNI152NLin2009cAsym, ...) is the dataset's statement. Another label
    (e.g. "user<Id>" of a user template, as ``normalize --template user:<id>`` names its outputs) is resolved through
    the file's sidecar; a user-template label without one keeps the label: the lesion is on that template's own grid.
    """
    from .geometry import space_family

    space = (bids_entities(path) or ({}, None))[0].get("space")
    if not space:
        return None
    family = space_family(space)
    if family == "native":
        return None
    if family != "unknown":
        return space
    return _sidecar_space(path) or (space if _USER_SPACE.match(space) else None)


def find_bids_lesions(input_dir, skip=()):
    """{participant_id: [lesion paths, the chosen one first]} of a BIDS dataset (hidden folders and ``skip`` are not searched)."""
    input_dir = os.path.abspath(input_dir)
    skip = {os.path.abspath(path) for path in skip if path}
    found = {}
    for folder, subfolders, files in os.walk(input_dir):
        subfolders[:] = sorted(d for d in subfolders if not d.startswith(".") and os.path.join(folder, d) not in skip)
        for name in sorted(files):
            path = os.path.join(folder, name)
            if is_lesion_file(path):
                found.setdefault(f"sub-{bids_entities(path)[0]['sub']}", []).append(path)
    return {sid: sorted(paths, key=_choice_key) for sid, paths in sorted(found.items())}


def normalize_labels(labels):
    """Participant ids ("sub-<label>") of --participant-label values given with or without "sub-"."""
    if not labels:
        return None
    out = []
    for label in labels:
        for item in str(label).replace(",", " ").split():
            out.append(item if item.startswith("sub-") else f"sub-{item}")
    return sorted(set(out))


def prepare_bids_study(input_dir, output_dir, group_column=None, group_map=None, score_columns=(), emit=None):
    """Create or update the study of a BIDS dataset in ``<output_dir>/sourcedata/slicer-lnm-study``; return (study path, summary).

    Only the lesion masks (:func:`is_lesion_file`) are added, one per subject (template space first; the
    others are reported), and only the participants.tsv rows of those subjects are imported.
    """
    from . import pipeline
    from .study import Study, is_missing

    study_path = os.path.join(os.path.abspath(output_dir), *STUDY_RELPATH)
    lesions = find_bids_lesions(input_dir, skip=[output_dir])
    if not lesions:
        raise ValueError(f"No lesion mask (*_label-lesion_mask, *_desc-lesion_mask or *_label-lesion_roi .nii[.gz]) in {input_dir}")
    chosen = [paths[0] for paths in lesions.values()]
    skipped = [{"participant_id": sid, "path": os.path.relpath(path, input_dir).replace(os.sep, "/"), "reason": "another lesion mask was chosen"}
               for sid, paths in lesions.items() for path in paths[1:]]
    participants = os.path.join(input_dir, "participants.tsv")
    with tempfile.TemporaryDirectory(prefix="slicer-lnm-") as tmp:
        table = None
        if os.path.isfile(participants):
            columns, rows = pipeline._read_table(participants)
            id_column = "participant_id" if "participant_id" in columns else columns[0]
            kept = [row for row in rows if row.get(id_column) in lesions]
            if kept:
                table = os.path.join(tmp, "participants.tsv")
                with open(table, "w", encoding="utf-8", newline="") as f:
                    f.write("\t".join(columns) + "\n")
                    for row in kept:
                        f.write("\t".join(row.get(column, "") for column in columns) + "\n")
        summary = pipeline.init_study(chosen[0], study_path, participants=table, name=STUDY_NAME, score_columns=list(score_columns or ()), group_column=group_column, group_map=group_map,
                                      lesion_rule=LESION_RULE)

    study = Study.open(study_path)
    report = study.add_files(chosen, lesion_rule=LESION_RULE)
    # A chosen lesion mask that add_files could not read or use (corrupt, not 3D) is named in a warning and kept in the
    # subject's row, so that the pipeline excludes the subject as "lesion file unreadable", not as "lesion file missing"
    unusable = []
    chosen_paths = {os.path.abspath(path) for path in chosen}
    for item in report["skipped"]:
        path = os.path.abspath(item["path"])
        if path in chosen_paths and (item["reason"].startswith("unreadable") or item["reason"].startswith("not a 3D")):
            entities = bids_entities(path)[0]
            sid = f"sub-{entities['sub']}"
            relative = study.relpath(path)
            if sid not in study:
                study.add_subject(sid, {"lesion": relative, "status": "traced"})
            elif is_missing(study.subject(sid).get("lesion")):
                study.set(sid, lesion=relative)
            unusable.append({"path": path, "message": f"{os.path.relpath(path, input_dir).replace(os.sep, '/')}: lesion file cannot be used ({item['reason']})", "participant_id": sid})
    for path in chosen:
        # The BIDS space entity is the dataset's statement: a template space (MNI152NLin2009cAsym, ...) is not re-detected
        # from the grid, and a user template's space (space-user<Id>, whose grid is not a standard one) is not read as native
        sid, space = f"sub-{bids_entities(path)[0]['sub']}", bids_lesion_space(path)
        if space and study.subject(sid).get("space") != space:
            study.set(sid, space=space)
    study.save()
    for item in report["warnings"]:
        summary.setdefault("warnings", []).append(item)
    for item in unusable:
        summary.setdefault("warnings", []).append({"path": item["path"], "message": item["message"]})
    summary["lesion_files"] = len(chosen)
    summary["other_lesion_files"] = skipped
    if emit is not None:
        emit("info", code="bids_study", message=f"BIDS input: {len(chosen)} lesion masks; study {os.path.join(*STUDY_RELPATH)} ({len(study)} subjects)")
        for item in unusable:
            emit("warning", code="bids_lesion_unreadable", participant_id=item["participant_id"], message=item["message"])
        for item in skipped:
            emit("warning", code="bids_lesion_skipped", participant_id=item["participant_id"], message=f"{item['path']}: {item['reason']}")
    return study_path, summary


# Pipeline configuration


def _json_value(text, what):
    """A JSON value from text or from the JSON file it names."""
    if isinstance(text, str) and os.path.isfile(text):
        with open(text, encoding="utf-8") as f:
            return json.load(f)
    try:
        return json.loads(text)
    except ValueError:
        raise ValueError(f"{what} is neither JSON nor a JSON file: {text!r}") from None


def design_options(path):
    """PipelineConfig fields of a design.json (format slicer-lnm-design) that the pipeline's GLM and sLNM stages can honour.

    :raises ValueError: for a design the pipeline cannot run (another group column or levels, an ANOVA,
      transforms, blocks, permutation scheme or variance groups); run ``slicer-lnm glm --design`` for those.
    """
    from . import design

    spec = design.DesignSpec.load(path)
    unsupported = []
    if spec.test == "anova":
        unsupported.append("test anova")
    if spec.test == "two_group":
        if (spec.group_column or "group") != "group":
            unsupported.append(f"group_column {spec.group_column}")
        if spec.levels and [str(level) for level in spec.levels] != ["case", "control"]:
            unsupported.append(f"levels {spec.levels}")
        if spec.contrast != "two_sided":
            unsupported.append(f"contrast {spec.contrast}")
    if spec.test == "continuous" and spec.contrast != "two_sided":
        unsupported.append(f"contrast {spec.contrast}")
    for key, default in (("transforms", {}), ("block_column", None), ("permute", "auto"), ("variance_groups", False), ("restrict_to_mixed_blocks", False)):
        value = getattr(spec, key)
        if value and value != default:
            unsupported.append(f"{key} {value}")
    if unsupported:
        raise ValueError(f"The pipeline's GLM does not support {', '.join(unsupported)} (not available in this build through slicer-lnm); run 'slicer-lnm glm --design {path}' on the maps")
    options = {"covariates": list(spec.covariates), "input": "AvgFz" if spec.input == "Fz" else "T"}
    if spec.volume_off_reason:
        options["volume_off_reason"] = spec.volume_off_reason
    inference = spec.inference or {}
    for key, field in (("n_perm", "perm"), ("seed", "perm_seed"), ("tfce", "tfce"), ("fdr", "fdr")):
        if inference.get(key) is not None:
            options[field] = inference[key]
    if spec.test == "two_group":
        options["specificity"] = "glm"
    elif spec.test == "continuous":
        options["scores"] = [spec.score_column]
    elif spec.test == "one_sample":
        options["specificity"] = "off"
    return options


#: argparse destination -> PipelineConfig field of the options passed through as they are
_PASSTHROUGH = ("preview_n", "preview_seed", "sensitivity", "specificity", "nulls", "null_seed", "perm_seed", "alpha", "scores", "native", "include_unreviewed", "strict", "memory_budget_gb",
                "template", "atlas", "atlas_labels", "figure_convention", "recompute", "build_references", "slicer", "nice")


def build_config(args, study_path, emit=None):
    """The :class:`pipeline.PipelineConfig` of a level run: recipe, --pipeline-config, --design, then the explicit options."""
    from . import pipeline

    values = {}
    if args.recipe:
        from . import recipes

        recipe = recipes.find_recipe(args.recipe, study=study_path)
        if recipe is None:
            raise ValueError(f"Unknown recipe {args.recipe!r}")
        converted = recipes.to_pipeline_config(recipe, study_path)
        values.update(converted["config"])
        outside = converted.get("outside_pipeline") or []
        # pipeline.json "recipe" (read by methods and the report), with the steps this run does not do
        values["recipe"] = {**converted["recipe"], "outside_pipeline": [step["id"] for step in outside]}
        if emit is not None and converted.get("unavailable"):
            emit("info", code="recipe_steps", message=f"Recipe {recipe.id}: not available in this build: {', '.join(converted['unavailable'])}")
        if emit is not None:
            for step in outside:  # never skipped silently (as 'slicer-lnm recipes config' says)
                emit("info", code="recipe_step_outside", step=step["id"], message=f"Recipe {recipe.id}: step {step['id']} ({step['label']}) is not part of the pipeline: run it from {step['where']}")
    if args.pipeline_config:
        values.update(pipeline.PipelineConfig.load(args.pipeline_config).to_json())
    if args.design:
        values.update(design_options(args.design))
    for name in _PASSTHROUGH:
        value = getattr(args, name, None)
        if value is not None and value is not False:
            values[name] = value
    if args.rule is not None:
        values["rule"] = _json_value(args.rule, "--rule")
    if args.null_relocations is not None:
        values["null_relocations"] = args.null_relocations
    if args.perm is not None:
        values["perm"] = args.perm
    if args.no_report:
        values["report"] = False
    if args.threads:
        values["threads"] = args.threads
    for key in ("template", "atlas", "atlas_labels"):
        if values.get(key):
            values[key] = os.path.abspath(values[key])
    values.update({
        "study": os.path.abspath(study_path),
        "bundle": os.path.abspath(args.connectome),
        "accept_terms": bool(args.accept_terms),
        "events": args.events,
        "software": {"name": PROG, "version": version()},
        "parent_pid": None,
    })
    values.pop("out", None)
    try:
        return pipeline.PipelineConfig.from_json(values)
    except ValueError as exc:
        if "--no-volume-covariate" not in str(exc):
            raise
        # The shared pipeline message names the 'pipeline run' option, which slicer-lnm does not have.
        where = "volume_off_reason in the design file" if args.design else "volume_off_reason in the pipeline configuration"
        raise ValueError(f"Turning the lesion-volume covariate off needs a reason: set {where}, or keep volume_ml among the covariates") from None


# Level runs


def _runner_class():
    from . import pipeline

    class LevelRunner(pipeline.Runner):
        """A pipeline run restricted to a BIDS-App level and to the selected participants."""

        def __init__(self, config, emit, cancel, argv, level, labels):
            super().__init__(config, emit, cancel, argv)
            self.level = level
            self.labels = set(labels) if labels else None
            self._selected = False

        def _select(self):
            if self.labels is None or self._selected:
                return
            self._selected = True
            dropped = []
            for row in self.rows:
                if row["participant_id"] not in self.labels and row["status"] != "excluded":
                    row["status"], row["reason"] = "excluded", NOT_SELECTED
                    dropped.append(row["participant_id"])
            if dropped:
                self.info(f"{len(dropped)} subjects not selected by --participant-label", code="not_selected")

        def _snapshot(self, rows):
            self._select()
            super()._snapshot([row for row in rows if row["status"] in ("compute", "reuse")])

        def _stage(self, name):
            if self.level == "participant" and name not in PARTICIPANT_STAGES:
                provider = self.providers[name]
                now = pipeline._now_iso()
                self.current_outputs = []
                self.stage_records[name] = {"name": name, "label": pipeline.STAGE_LABELS[name], "provider": provider.module, "provider_kind": provider.kind, "provider_sha256": provider.sha256,
                                            "mode": pipeline._stage_mode(name), "start": now, "end": now, "elapsed": 0.0, "status": "skipped", "reason": DEFERRED, "reused": False, "outputs": {}}
                self.results[name] = {}
                self.fps[name] = f"skipped:{DEFERRED}"
                self.stage_event(name, "skipped", DEFERRED)
                self._write_pipeline_json("running")
                return
            super()._stage(name)

    return LevelRunner


def run_level(config, level, labels=None, emit=None, cancel=None, argv=None):
    """Run one BIDS-App level of the pipeline; return the exit code (see the module docstring)."""
    from . import pipeline

    runner = _runner_class()(config, emit if emit is not None else pipeline.Emitter(config.events), cancel, argv, level, labels)
    try:
        return runner.run()
    except pipeline.PipelineError as exc:
        runner.emit("error", code=exc.code, message=exc.message, **exc.fields)
        return exc.exit_code


def write_dataset_description(study_path, output_dir):
    """Copy the application's ``<study>/derivatives/lnm/dataset_description.json`` to ``output_dir`` (kept when present); return its path or None."""
    from . import pipeline

    source = os.path.join(study_path, "derivatives", "lnm", pipeline.DATASET_DESCRIPTION)
    target = os.path.join(output_dir, pipeline.DATASET_DESCRIPTION)
    if os.path.exists(target) or not os.path.isfile(source):
        return target if os.path.exists(target) else None
    if os.path.realpath(source) != os.path.realpath(target):
        tmp = f"{target}.{os.getpid()}.tmp"
        shutil.copyfile(source, tmp)
        os.replace(tmp, target)
    return target


def render_figures(presets, out, config, emit):
    """Figures of the group mean T map (``figures`` presets) in ``<out>/figures/slicer-lnm-<preset>/``; return the folders written."""
    import contextlib
    import io

    from . import export, figures, jobs, pipeline
    from .connectome import ConnectomeBundle

    bundle = ConnectomeBundle.open(config.bundle)
    data = pipeline._read_json(os.path.join(out, pipeline.PIPELINE_JSON)) or {}
    group_map = export.group_map_path(out, bundle.name, FIGURE_MAP_DESC)
    written = []
    for preset in presets:
        args = ["--preset", preset, "--out", out, "--study", config.study, "--connectome", bundle.name, "--name", f"{PROG}-{preset}", "--run-root", config.study]
        if preset == "gallery":
            lesions = [pipeline.abspath(row["snapshot"], out) for row in data.get("inputs", {}).get("rows", []) if row.get("snapshot")]
            if not lesions:
                emit("warning", code="figure", message=f"Figure {preset}: no analysed lesion")
                continue
            for path in lesions:
                args += ["--lesion", path]
        else:
            if not os.path.isfile(group_map):
                emit("warning", code="figure", message=f"Figure {preset}: the group map {os.path.basename(group_map)} was not written")
                continue
            args += ["--map", group_map, "--mask", os.path.join(out, pipeline.PRIVATE_DIR, pipeline.MASK_NAME), "--statistic", "mean T", "--title", "Mean T across lesions (descriptive)"]
        if config.template:
            args += ["--template", config.template]
        if config.figure_convention:
            args += ["--convention", config.figure_convention]
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer):
                figures.main(args)
        except (jobs.JobError, ImportError, OSError, ValueError) as exc:
            emit("warning", code="figure", message=f"Figure {preset} not written: {getattr(exc, 'message', None) or exc}")
            continue
        folder = os.path.join(out, "figures", f"{PROG}-{preset}")
        written.append(folder)
        emit("info", code="figure", message=f"Figure {preset}: {os.path.relpath(folder, out)}")
    return written


def _wait_for_lock(out, wait_s, emit):
    """Wait until no pipeline run holds ``out`` (at most ``wait_s`` seconds); return True when it is free."""
    from . import jobs, pipeline

    lock = jobs.OutputLock(os.path.join(out, pipeline.PRIVATE_DIR, pipeline.LOCK_NAME))
    deadline = time.monotonic() + max(float(wait_s or 0), 0.0)
    told = False
    while True:
        holder = lock.read()
        if not os.path.exists(lock.path) or lock.is_stale(holder):
            return True
        if time.monotonic() >= deadline:
            return False
        if not told:
            emit("info", code="waiting_for_lock", message=f"Another run (process {(holder or {}).get('pid')}) writes to {os.path.basename(out)}: waiting")
            told = True
        time.sleep(LOCK_POLL_S)


# Command line


def _parser(commands=()):
    from . import jobs, pipeline

    epilog = "library jobs: " + ", ".join(commands) + f"  (run '{PROG} <command> --help')" if commands else None
    parser = jobs.JsonArgumentParser(prog=PROG, description="Lesion network mapping BIDS-App (Slicer Studio's LesionNetworkMappingLib). " + "Research use only.", epilog=epilog)
    parser.add_argument("input_dir", help="BIDS dataset with lesion masks (*_label-lesion_mask, *_desc-lesion_mask, *_label-lesion_roi), or a Slicer Studio study folder")
    parser.add_argument("output_dir", help="output folder (BIDS derivatives: dataset_description.json and conn-<label>[_n-<N>]/)")
    parser.add_argument("analysis_level", choices=LEVELS, help="participant: maps and per-lesion QA; group: every cohort stage")
    parser.add_argument("--connectome", required=True, help="connectome bundle (.lnmc folder; may be mounted read-only)")
    parser.add_argument("--accept-terms", action="store_true", help="accept the data use terms of the connectome (required)")
    parser.add_argument("--participant-label", nargs="+", default=None, help="subjects to analyse (with or without 'sub-'); default: all")
    parser.add_argument("--rule", default=None, help="primary threshold rule as JSON or a JSON file (default FWE alpha 0.05)")
    parser.add_argument("--recipe", default=None, help="analysis recipe (id or file); options given here override it")
    parser.add_argument("--design", default=None, help="design.json (format slicer-lnm-design) for the GLM and sLNM stages")
    parser.add_argument("--subject-list", default=None, help="connectome subject list (not available in this build: the pipeline maps all subjects or --preview N)")
    parser.add_argument("--null-relocations", default=None, help="per-lesion relocation nulls: auto (50 / 10 / 0 by cohort size) or K")
    parser.add_argument("--perm", type=int, default=None, help=f"permutations of the GLM and sLNM (at least {pipeline.MIN_PERM}; default {pipeline.DEFAULT_PERM})")
    parser.add_argument("--figures", default=None, help=f"group level: extra figures of the mean T map, comma-separated presets ({', '.join(FIGURE_PRESETS)})")
    parser.add_argument("--group-column", default=None, help="BIDS input: participants.tsv column holding the group")
    parser.add_argument("--group-map", default=None, help="BIDS input: group values, e.g. 'patient=case,HC=control'")
    parser.add_argument("--score-columns", default="", help="BIDS input: comma-separated participants.tsv columns confirmed as scores (sLNM)")
    parser.add_argument("--preview", dest="preview_n", type=int, default=None, help="map with N randomly chosen connectome subjects (maps labelled PreviewN<N>)")
    parser.add_argument("--preview-seed", dest="preview_seed", type=int, default=None)
    parser.add_argument("--sensitivity", type=float, default=None, help="sensitivity map: percent of the maps (50-100; default 90)")
    parser.add_argument("--specificity", choices=pipeline.SPECIFICITY_MODES, default=None)
    parser.add_argument("--nulls", choices=pipeline.NULL_MODES, default=None, help="cohort null models (group level)")
    parser.add_argument("--null-seed", type=int, default=None)
    parser.add_argument("--perm-seed", type=int, default=None)
    parser.add_argument("--alpha", type=float, default=None)
    parser.add_argument("--scores", default=None, help="auto (confirmed score columns), none, or a comma-separated list")
    parser.add_argument("--native", choices=pipeline.NATIVE_MODES, default=None)
    parser.add_argument("--include-unreviewed", action="store_true", default=None)
    parser.add_argument("--strict", action="store_true", default=None)
    parser.add_argument("--memory-budget-gb", type=float, default=None)
    parser.add_argument("--template", default=None, help="background template NIfTI of the figures and the report")
    parser.add_argument("--atlas", default=None)
    parser.add_argument("--atlas-labels", default=None)
    parser.add_argument("--figure-convention", default=None, help="neurological (default) or radiological")
    parser.add_argument("--build-references", action="store_true", default=None, help="build missing reference maps (needs a writable connectome folder)")
    parser.add_argument("--no-report", action="store_true", help="do not render report.html")
    parser.add_argument("--recompute", action="store_true", default=None, help="ignore the saved stages and the map cache")
    parser.add_argument("--pipeline-config", default=None, help="pipeline configuration JSON (or a pipeline.json); the options here override it")
    parser.add_argument("--slicer", default=None, help="Slicer executable for stages that need the application (normalization)")
    parser.add_argument("--nice", type=int, default=None)
    parser.add_argument("--events", choices=pipeline.EVENT_MODES, default="auto", help="auto (text on a terminal), json or text")
    parser.add_argument("--lock-wait", type=float, default=24 * 3600.0, help="seconds to wait for another run writing to the same output folder (default one day)")
    parser.add_argument("--threads", type=jobs.positive_int, default=None, help="worker and BLAS threads (default: CPUs - 1, capped by $SLICER_LNM_MAX_THREADS)")
    parser.add_argument("--cancel-file", default=None, help="stop (exit code 3) as soon as this file exists")
    parser.add_argument("--version", action="version", version=f"{PROG} {version()}")
    return parser


def _main(argv):
    from . import jobs, pipeline

    args = _parser(library_commands()).parse_args(argv)
    emit = pipeline.Emitter(args.events)
    if args.subject_list:
        emit("error", code="usage", message="--subject-list is not available in this build: the cohort pipeline maps every connectome subject, or --preview N random subjects")
        return jobs.EXIT_USAGE
    presets = [item.strip() for item in (args.figures or "").split(",") if item.strip()]
    unknown = sorted(set(presets) - set(FIGURE_PRESETS))
    if unknown:
        emit("error", code="usage", message=f"Unknown figure preset(s) {', '.join(unknown)}; expected {', '.join(FIGURE_PRESETS)}")
        return jobs.EXIT_USAGE
    input_dir, output_dir = os.path.abspath(args.input_dir), os.path.abspath(args.output_dir)
    if not os.path.isdir(input_dir):
        emit("error", code="usage", message=f"The input folder does not exist: {args.input_dir}")
        return jobs.EXIT_USAGE
    if not os.path.isdir(args.connectome):
        emit("error", code="usage", message=f"The connectome bundle does not exist: {args.connectome}")
        return jobs.EXIT_USAGE
    for option in ("template", "atlas", "atlas_labels"):
        path = getattr(args, option)
        if path is not None and not os.path.isfile(path):
            emit("error", code="usage", message=f"The file of --{option.replace('_', '-')} does not exist: {path}")
            return jobs.EXIT_USAGE
    problem = pipeline.figure_option_error(args.template, args.figure_convention)
    if problem:
        emit("error", code="usage", message=problem)
        return jobs.EXIT_USAGE
    from .connectome import ConnectomeBundle
    from .export import run_dir_name
    from .study import Study

    try:
        bundle = ConnectomeBundle.open(args.connectome)
    except (OSError, ValueError) as exc:
        emit("error", code="bundle", message=f"The connectome cannot be opened: {exc}")
        return jobs.EXIT_ERROR
    os.makedirs(output_dir, exist_ok=True)
    try:
        if Study.is_study(input_dir):
            study_path = input_dir
            for option in ("group_column", "group_map"):
                if getattr(args, option):
                    emit("warning", code="study_input", message=f"--{option.replace('_', '-')} applies to a BIDS input; the study's own columns are used")
        else:
            study_path, _summary = prepare_bids_study(input_dir, output_dir, args.group_column, args.group_map, [name.strip() for name in args.score_columns.split(",") if name.strip()], emit)
        labels = normalize_labels(args.participant_label)
        if labels:
            missing = sorted(set(labels) - set(Study.open(study_path).subject_ids))
            if missing:
                emit("error", code="usage", message=f"Participant label(s) not in the input: {', '.join(missing)}")
                return jobs.EXIT_USAGE
        config = build_config(args, study_path, emit)
    except jobs.Locked as exc:
        emit("error", code="locked", message=exc.message)
        return jobs.EXIT_LOCKED
    except (OSError, ValueError, TypeError, KeyError) as exc:
        emit("error", code="usage", message=f"{type(exc).__name__}: {exc}")
        return jobs.EXIT_USAGE
    out = os.path.join(output_dir, run_dir_name(bundle.name, config.preview_n))
    config.out = out
    if not _wait_for_lock(out, args.lock_wait, emit):
        emit("error", code="locked", message=f"Another run still writes to {os.path.basename(out)} after {args.lock_wait:g} s")
        return jobs.EXIT_LOCKED
    code = run_level(config, args.analysis_level, labels, emit=emit, cancel=args.cancel_file, argv=argv)
    if code == jobs.EXIT_OK:
        write_dataset_description(study_path, output_dir)
        if presets and args.analysis_level == "group":
            render_figures(presets, out, config, emit)
    return code


def main(argv=None):
    """Console entry point of ``slicer-lnm``; returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if argv and argv[0] in ("--version", "-V"):
        sys.stdout.write(f"{PROG} {version()}\n")
        return 0
    if argv and not argv[0].startswith("-") and argv[0] in library_commands():
        return run_library_command(argv[0], argv[1:])
    from .jobs import run_job, set_blas_threads

    set_blas_threads(argv)  # before numpy is imported: BLAS reads these variables once
    return run_job(_main, argv, job=PROG, exit=False)


if __name__ == "__main__":
    sys.exit(main())

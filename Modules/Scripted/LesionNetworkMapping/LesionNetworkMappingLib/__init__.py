"""Lesion network mapping core library (numpy and scipy only; no Slicer, VTK or Qt imports).

Submodules:

- nifti: minimal NIfTI-1 reader and writer that keeps exact voxel order and affines
- geometry: voxel grids, affine-based resampling (fractional occupancy), masked vectors
- connectome: ".lnmc" connectome bundles, converters, synthetic data, degree maps
- engine: streaming network-mapping engine and the ``python -m LesionNetworkMappingLib.engine`` CLI
- stats: thresholds, overlap, specificity tests, symptom mapping and QA
- thresholds: threshold rules re-resolved from N and V (FWE, |T| convention, Fz effect size, custom T)
- qa: connectome-degree similarity, relocated-lesion null and the ``python -m LesionNetworkMappingLib.qa`` CLI
- study: plain-file lesion studies (study.json and BIDS-style participants.tsv)
- export: map export with JSON sidecars, provenance.json, methods.md and reproduce.sh
- design: GLM design types, contrasts, checks and permutation schemes (``design.json``)
- glm: Freedman-Lane permutation GLM with max-statistic FWE, BH-FDR and effect sizes
- tfce: threshold-free cluster enhancement on the connectome mask
- slnm: symptom (score) network mapping: rank maps, partial r, nulls, leave-one-dataset-out
- nulls: null models: relocated lesions, strength-matched and annealed overlap nulls
- lesionset: lesion occupancy on a template grid, overlap maps, Dice, tissue fractions
- lesionops: lesion intake numerics: dimensionality, extrusion, mirror, hygiene, side, CSF share, duplicate index
- cohort: group maps of a cohort, conjunctions and the cohort command line
- cohortmaps: per-lesion map batches with a content-keyed cache
- preflight: header-only checks of a study, connectome and design before a run
- normalize: normalization of native-space lesions to MNI (ANTs, a check and a batch)
- normqc: normalization QC metrics (overlap, Jacobian, volume change, mosaic)
- review: review states, tokens, audit log and the review gate
- agreement: second tracings, inter-rater metrics and adjudication
- sources: literature records, screening stages and PRISMA counts and flow diagram
- share: share export of a study (lesions, maps, scrubbed headers, validator)
- schema: participants.tsv column registry and the data dictionary
- catalog: connectome catalog (licence badges, terms, subsets, disk plan)
- fetch: verified, resumable downloads of templates, atlases and other resources
- fetchconnectome: "Get a connectome": terms-gated streaming download and conversion
- leaddbs: Lead-DBS interoperability: map names, imports and the parity check
- refmaps: reference-map library, comparison and permutation p-values of in-study maps
- tutorials: tutorial cohorts that build ready Studies
- vlsm: voxel-based lesion-symptom mapping on the cohort's binary lesion masks
- circuits: circuits seeded from maps or clusters, damage scores, association checks
- scores: per-lesion scores (damage, similarity, ROI connectivity) as matrix products
- similarity: Pearson and Spearman similarity and partial spatial correlation
- robustness: threshold sweeps, split-half, leave-one-out and cohort replication
- validate: leakage-safe cross-validation of a map-based predictor
- calibrate: empirical FWER and sensitivity calibration of a design
- clusters: cluster tables with atlas labels and homologues
- atlases: atlas labels for points, masks and clusters
- coords: coordinates, foci, atrophy and stimulation seeds
- controlsets: control sets (registry, maps and covariate matching)
- fastmap: approximate maps from a parcel-based fast index
- figurefit: landmark fit of a published figure to a template cut plane
- figures: figure rendering (montages, triptychs, glass brains) with provenance
- jobs: job protocol: JSON events, cancel, locks, keep-awake, common arguments
- manager: connectome manager helpers and conversion estimates
- methods: methods text sections of a run or a pipeline
- report: Markdown and HTML report rendering with wording lint
- reportfigs: figures of the report (SVG plots and PNG montages)
- pipeline: the automated cohort analysis (Run everything)
- recipes: analysis recipes, the pre-registered plan and its deviations
- reference: reference specification (seed selection, external maps)
- roi: ROI statistics and small-volume FWE
- palm: PALM and randomise exports and imports
- runs: the run registry (runs.jsonl) and lineage
- transforms: spatial transforms (reading, direction, warping)
- app: the headless twin (``python -m LesionNetworkMappingLib.app``)
- colormaps: the diverging colour map shared by the module and its figures
- display: MRML display helpers (needs the Slicer application; import it directly, it is not in the lazy list)

The public names below are imported lazily on first access, so importing the package (at Slicer
startup, or before the engine CLI sets the BLAS thread count) does not import numpy or scipy.
"""

import importlib

_EXPORTS = {
    "nifti": ["read_nifti", "read_nifti_header", "write_nifti", "write_like", "xform_disagreement"],
    "geometry": [
        "Grid",
        "resample_to_grid",
        "mask_indices",
        "to_masked_vector",
        "from_masked_vector",
        "voxel_to_world",
        "world_to_voxel",
        "affine_from_spm_mat",
        "signed_permutation",
        "detect_space",
        "detect_grid",
    ],
    "connectome": [
        "ConnectomeBundle",
        "ConnectomeWriter",
        "SubjectData",
        "UnsupportedFormatError",
        "from_nifti_timeseries",
        "from_leaddbs",
        "make_synthetic",
        "compute_degree",
        "sanitize_subject_id",
    ],
    "engine": [
        "NetworkMaps",
        "compute_network_maps",
        "subject_correlation",
        "prepare_lesion",
        "lesion_weights",
        "lesion_qc",
        "unmask",
        "resolve_subjects",
        "output_paths",
        "write_maps",
        "benchmark",
        "connectome_label",
        "null_voxel_sample",
    ],
    "stats": [
        "t_to_p",
        "p_to_t",
        "bonferroni_t_threshold",
        "threshold_map",
        "binarize",
        "overlap",
        "sensitivity_map",
        "conjunction",
        "welch_t",
        "liebermeister",
        "permutation_maxstat_fwe",
        "symptom_correlation",
        "spatial_correlation",
        "peak_table",
    ],
    "thresholds": ["Resolved", "DEFAULT_RULE", "normalize_rule", "resolve", "describe", "rule_to_json", "rule_from_json", "rule_label"],
    "qa": ["degree_similarity", "relocated_lesions", "null_summary", "chip_level", "null_level", "lesion_qa", "describe_null"],
    "study": ["Study", "participant_label"],
    "export": ["DISCLAIMER", "CITATIONS", "write_run", "export_run", "sidecar_path", "map_paths", "derivatives_dir", "group_map_path", "write_peaks_tsv"],
    "design": ["DesignError", "DesignSpec", "Design", "build_design", "design_preview", "design_shufflings", "read_design_json", "save_design"],
    "glm": ["GlmResult", "FreedmanLane", "NullState", "permutation_test", "fwe_from_null", "effect_size", "contrast_label"],
    "tfce": ["TfceGrid", "tfce_eta"],
    "slnm": ["SlnmDesign", "slnm_design", "adjust_design", "rank_maps", "partial_r_map", "ScoreNull", "lodo", "stouffer"],
    "nulls": ["Relocator", "relocate_independent", "relocate_strength", "NullOverlap", "RelocationPool", "build_relocation_pool", "load_null_group_maps", "anneal_overlap", "volume_matched_sets"],
    "lesionset": ["LesionSet", "GridChoice", "resolve_grid", "load_occupancies", "OverlapMaps", "overlap_maps", "write_overlap", "dice_matrix", "tissue_fractions"],
    "lesionops": ["detect_dimensionality", "extrude", "mirror_world", "hygiene", "side_check", "csf_share", "CsfShare", "LesionIndex", "qc_flags", "normalize_side"],
    "cohort": ["GroupMaps", "group_maps", "write_group_maps", "lesions_from", "maps_from_dir"],
    "cohortmaps": ["plan_batches", "cache_key", "read_lesions_tsv", "read_maps_tsv", "write_maps_tsv", "study_lesions"],
    "preflight": ["run_preflight", "blocking_checks", "assert_run_outputs", "disk_estimate", "save_report", "measure_lesion"],
    "normalize": ["template_grid", "find_executable", "executable_version", "guess_modality", "output_names", "is_reusable"],
    "normqc": ["brain_mask_dice", "ncc", "nmi", "lesion_volume_change", "log_jacobian_lesion_minus_brain", "compute_metrics", "normqc_json", "qc_mosaic_png"],
    "review": ["TransitionError", "apply_transition", "validate_transition", "allowed_transitions", "gate_summary", "record_override", "blinded_row", "append_audit", "read_audit"],
    "agreement": ["AgreementError", "pair_metrics", "between_lesion_baseline", "consensus_mask", "adjudicate", "undo_adjudication", "save_second_tracing", "hd95", "surface_distances", "second_tracing_todo"],
    "sources": ["SourcesError", "parse_nbib", "parse_ris", "parse_bibtex", "parse_identifiers", "parse_text", "detect_format", "add_cases", "prisma_counts", "prisma_flow_svg", "export_prisma", "import_text"],
    "share": ["ShareError", "export_dataset", "validate_dataset", "verify_export", "scrub_nifti", "find_absolute_paths", "neurovault_folder", "pseudonyms"],
    "schema": ["ColumnSpec", "register_column", "normalize_value", "infer_spec", "dictionary_entry", "write_dictionary", "read_dictionary"],
    "catalog": ["ConnectomeCatalog", "licence_badge", "terms_sha256", "normalize_terms_text", "resolve_subset", "disk_plan", "validate_catalog"],
    "fetch": ["FetchError", "OfflineError", "ChecksumError", "Resource", "find_resource", "badge_for", "fetch_set"],
    "fetchconnectome": ["fetch_connectome", "read_live", "live_terms", "check_fetchable", "compare_catalog", "plan_files", "SignedUrlResolver", "ArchiveSource", "unfinished_fetch", "is_web_build", "default_destination", "destination_notice", "terms_display_text"],
    "leaddbs": ["import_leaddbs_maps", "export_leaddbs_names", "run_parity", "parity_metrics", "load_parity_catalog", "is_leaddbs_release", "parse_funcmap_name", "top_fraction_dice"],
    "refmaps": ["RefmapsError", "NotInStudyError", "add_neurovault", "add_vdh", "add_url", "add_file", "load_registry", "save_registry", "resample_to", "InStudyMap", "build_in_study", "Comparison", "p_reason", "top_similar", "heatmap_figure", "write_outputs", "remove_entry", "vdh_catalog"],
    "tutorials": ["TutorialError", "build_tutorial", "build_study", "tutorial_info", "is_tutorial", "watermark_for_study", "unique_candidates", "cache_bytes", "clear_cache", "human_size"],
    "vlsm": ["LesionMatrix", "lesion_matrix", "min_lesion_count", "min_lesion_rule", "clusters_of_result", "cluster_roi", "seed_cluster", "find_results", "load_lesions", "lesion_rows", "write_lesions_tsv"],
    "circuits": ["CircularityError", "seed_this", "read_circuit", "find_circuits", "damage_scores", "check_association", "association", "score_targets", "circuit_name", "binary_roi", "sphere_mask", "residualize"],
    "scores": ["damage_score", "mean_in_lesion", "pct_lesion_in_circuit", "similarity_score", "similarity_matrix", "damage_matrix", "roi_matrix"],
    "similarity": ["pearson", "spearman", "pearson_spearman", "partial_spatial_correlation", "residual_map", "r2_explained", "ranks"],
    "robustness": ["threshold_sweep", "conclusion_flips", "compare_cohorts", "lesion_split_half", "leave_one_lesion_out", "connectome_split_half", "seed_variants", "peak_persistence", "replicate_on", "SufficientStatistics"],
    "validate": ["LeakageError", "assert_disjoint", "Folds", "make_folds", "lodo_check", "infer_outcome_type", "ValidationResult", "ValidationData", "apply_frozen", "frozen_metrics", "verdict_text"],
    "calibrate": ["NotCalibratable", "clopper_pearson", "empirical_fwer", "sensitivity_fpr", "planted_network", "fwer_verdict", "rate_summary", "estimate_seconds", "permutation_seconds", "null_studies"],
    "clusters": ["ClusterTable", "cluster_table", "atlas_labeler", "pair_homologues", "cluster_caption", "write_clusters"],
    "atlases": ["Atlas", "LabelRow", "LabelResult", "label_point", "label_points", "label_mask", "label_cluster", "load_cached", "network_of", "to_yeo7", "read_label_table", "mask_family"],
    "coords": ["Focus", "foci_from_rows", "read_foci", "read_sleuth", "read_nimare_json", "mni2tal", "tal2mni", "to_mni", "sphere_occupancy", "spheres", "random_coordinate_null", "prepare_coordinates", "prepare_atrophy", "prepare_stimulation", "atrophy_seed", "stimulation_seed", "write_seed_set", "read_seed_record"],
    "controlsets": ["AmbiguousSpaceError", "list_sets", "register_folder", "set_lesions", "map_controls", "study_control_lesions", "mismatch_report", "unregister", "content_sha256"],
    "fastmap": ["Tiling", "FastIndex", "build_tiling", "build_index", "load_index", "approximate_maps", "parcel_occupancy", "import_parcel_matrix", "compare_parcel_matrices", "lesion_allowed"],
    "figurefit": ["FigureFitError", "CutPlane", "FigureFit", "cut_plane", "fit_figure", "fit_2d", "apply_2d", "residual_chip", "make_record", "validate_record", "read_fit_json", "write_fit_json", "figure_ijk_to_ras", "image_sha256"],
    "figures": ["Figure", "Scene", "Layer", "Options", "MapInfo", "TileSpec", "PanelSpec", "load_volume", "display_grid", "reslice", "write_figure", "watermark_for", "triptych", "glass", "replication", "render_slice"],
    "jobs": ["JobError", "Cancelled", "Locked", "CancelToken", "KeepAwake", "OutputLock", "ResumeState", "run_job", "add_common_arguments", "write_json_atomic", "request_cancel", "budget_threads", "install_signal_handlers", "positive_int", "JsonArgumentParser"],
    "manager": ["bundle_summary", "provenance_line", "nifti_inputs", "estimate_conversion"],
    "methods": ["Section", "methods_sections", "run_sections", "find_section", "disclaimer_section", "software_text"],
    "report": ["ReportError", "render_report", "markdown_to_html"],
    "reportfigs": ["ReportFigure", "Canvas", "encode_png", "data_uri", "window_of", "colorbar_svg", "dot_plot_svg", "percentile_strip_svg", "gallery_strip", "colormap_note", "template_space"],
    "pipeline": ["PipelineError", "PipelineConfig", "Runner", "Provider", "resolve_providers", "estimate_maps", "estimate_glm", "fingerprint", "config_from_args", "init_study", "rule_tag", "unique_labels"],
    "recipes": [
        "RecipeError",
        "Recipe",
        "load_recipes",
        "find_recipe",
        "read_recipe",
        "validate_recipe",
        "available_steps",
        "lock_plan",
        "read_plan",
        "record_deviation",
        "read_deviations",
        "plan_status",
        "save_lab_recipe",
        "to_pipeline_config",
        "selected_recipe",
        "select_recipe",
        "compare_parameters",
        "describe_change",
    ],
    "reference": ["Reference", "load_reference", "reference_status", "select_seeds", "external_on_grid", "compare_to_external", "seeds_sha256", "subjects_data_sha256"],
    "roi": ["RoiError", "RoiResult", "roi_means", "roi_glm", "small_volume_fwe", "read_roi", "write_svc_mask"],
    "palm": ["palm_command", "randomise_command", "export_palm", "read_vest", "validate_palm_export", "import_palm", "read_csv_matrix"],
    "runs": ["RegistryWarning", "register_kind", "registry_path", "new_run_id", "canonical_json", "params_sha256", "run_dir", "list_runs", "read_records", "read_params", "lineage"],
    "transforms": ["DirectionError", "TransformDirection", "parse_transform_name", "transform_direction", "read_transform", "transform_kinds", "warp_image", "transform_points", "round_trip", "read_points_table", "ras_affine"],
    "app": ["library_commands", "run_library_command", "bids_entities", "is_lesion_file", "find_bids_lesions", "prepare_bids_study", "build_config", "run_level", "write_dataset_description", "render_figures", "design_options"],
    "colormaps": ["hex_to_rgb", "lnm_diverging", "entry_values", "entry_alphas"],
}
_MODULE_OF = {name: module for module, names in _EXPORTS.items() for name in names}

__all__ = list(_EXPORTS) + list(_MODULE_OF)


def __getattr__(name):
    if name in _EXPORTS:
        return importlib.import_module(f".{name}", __name__)
    module = _MODULE_OF.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

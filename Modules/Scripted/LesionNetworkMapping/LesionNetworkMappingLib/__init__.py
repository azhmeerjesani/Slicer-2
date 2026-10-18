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

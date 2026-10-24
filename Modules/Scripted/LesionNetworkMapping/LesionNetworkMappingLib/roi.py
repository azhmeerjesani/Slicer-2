"""A-priori ROI inference for lesion network mapping (numpy only; no Slicer imports).

When the regions of interest are stated before the analysis (a locked plan, recipes.py), the test does not
need to correct over the whole brain (addendum F23; Karoam 2025 OSF ROI; Cohen & Fox 2020):

- :func:`roi_means`: the mean of each map in each ROI mask (n x n_roi).
- :func:`roi_glm`: the design's GLM (design.py, glm.py: Freedman-Lane permutations, the design's exchange) on
  the ROI means, with the maximum statistic across ROIs as the family-wise correction: p_FWE of an ROI is
  (1 + #{max over ROIs of the null statistic >= its statistic}) / (K + 1). With one ROI it is the plain
  permutation p.
- :func:`small_volume_fwe`: the voxelwise GLM restricted to one ROI mask (small-volume correction): the voxel
  max-statistic is taken over the ROI's voxels only. The command line does the same through the GLM's
  ``--mask`` option: :func:`write_svc_mask` writes the ROI restricted to the connectome mask on the
  connectome grid, and ``python -m LesionNetworkMappingLib.glm --mask <that file> ...`` runs the test.

Both tests use the same permutations as the whole-brain GLM of the design (same seed), are two-sided for a
two-sided contrast, and run in float64 (the ROI tests are small), so their p-values are exact for the
shufflings drawn.

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage or blocked design)::

    python -m LesionNetworkMappingLib.roi glm --maps maps.tsv --participants participants.tsv --mask M
        --roi A.nii.gz [--roi B.nii.gz ...] [--design design.json] [--perm N] [--seed S] --out DIR
    python -m LesionNetworkMappingLib.roi svc-mask --roi A.nii.gz --mask M --out roi_mask.nii.gz

``glm`` writes ``roi_glm.json`` (per ROI: voxels, statistic, p_unc, p_FWE across ROIs, effect size) and
``roi_means.tsv`` in DIR. ROI masks must be on the connectome grid (the same shape and affine as the mask);
values above 0.5 are inside.
"""

import os
import sys

if not __package__:  # run as a script
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    __package__ = "LesionNetworkMappingLib"  # noqa: A001

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])

import dataclasses
import json

import numpy as np

FORMAT_NAME = "slicer-lnm-roi-glm"
FORMAT_VERSION = 1
ROI_GLM_JSON = "roi_glm.json"
ROI_MEANS_TSV = "roi_means.tsv"
#: Voxels of an ROI mask with a value above this are inside
INSIDE = 0.5
#: Affine entries may differ by this much between an ROI mask and the connectome mask
AFFINE_TOLERANCE = 1e-4


class RoiError(ValueError):
    """An ROI that cannot be used (empty, outside the mask, another grid)."""


def _as_masks(roi_masks, n_voxels):
    """ROI masks as a boolean (n_roi x V) array."""
    masks = np.atleast_2d(np.asarray(roi_masks))
    if masks.ndim != 2 or masks.shape[1] != n_voxels:
        raise RoiError(f"ROI masks must have {n_voxels} voxels each, got shape {np.shape(roi_masks)}")
    masks = masks > INSIDE if masks.dtype != bool else masks
    empty = [index for index, count in enumerate(masks.sum(axis=1)) if count == 0]
    if empty:
        raise RoiError(f"ROI {', '.join(str(index + 1) for index in empty)} has no voxel inside the mask")
    return masks


def roi_means(maps, roi_masks):
    """Mean of every map in every ROI: (n x n_roi), float64.

    :param maps: n x V maps (rows are subjects, in design order).
    :param roi_masks: one V-vector or an (n_roi x V) array of masks (bool, or weights > 0.5 inside).
    """
    maps = np.asarray(maps)
    if maps.ndim != 2:
        raise RoiError(f"maps must be n x V, got shape {maps.shape}")
    masks = _as_masks(roi_masks, maps.shape[1])
    out = np.empty((maps.shape[0], masks.shape[0]))
    for index, mask in enumerate(masks):
        out[:, index] = np.asarray(maps[:, mask], dtype=np.float64).mean(axis=1)
    return out


@dataclasses.dataclass
class RoiResult:
    """:func:`roi_glm` result: per ROI ``stat``, ``p_unc``, ``p_fwe`` (max-statistic across ROIs) and ``effect``."""

    names: list
    stat: np.ndarray
    p_unc: np.ndarray
    p_fwe: np.ndarray
    effect: np.ndarray
    effect_kind: str
    kind: str
    tails: int
    df: int
    n_perm: int
    min_p: float
    exhaustive: bool
    seed: int
    max_null: np.ndarray

    def to_dict(self, voxels=None):
        rows = []
        for index, name in enumerate(self.names):
            row = {"name": name, "stat": float(self.stat[index]), "p_unc": float(self.p_unc[index]), "p_fwe": float(self.p_fwe[index]), "effect": float(self.effect[index])}
            if voxels is not None:
                row["voxels"] = int(voxels[index])
            rows.append(row)
        return {
            "stat": self.kind,
            "tails": int(self.tails),
            "df": int(self.df),
            "n_perm": int(self.n_perm),
            "min_attainable_p": float(self.min_p),
            "exhaustive": bool(self.exhaustive),
            "seed": int(self.seed),
            "effect_size": self.effect_kind,
            "correction": "family-wise across ROIs: maximum statistic over the ROIs in every permutation",
            "rois": rows,
        }


def roi_glm(design, means, names=None, n_perm=None, seed=None, allow_blocked=False):
    """The design's permutation GLM on ROI means, family-wise corrected by the maximum statistic across ROIs.

    :param design: ``design.Design`` (rows in ``design.sids`` order).
    :param means: n x n_roi ROI means (:func:`roi_means`).
    :param names: ROI names (default "roi1", "roi2", ...).
    :param n_perm, seed: override the design's.
    :return: :class:`RoiResult`.
    """
    from . import glm

    means = np.asarray(means, dtype=np.float64)
    if means.ndim == 1:
        means = means[:, None]
    names = list(names) if names is not None else [f"roi{index + 1}" for index in range(means.shape[1])]
    if len(names) != means.shape[1]:
        raise RoiError(f"{len(names)} names for {means.shape[1]} ROIs")
    result = glm.run(design, means, n_perm=n_perm, seed=seed, tfce=False, dtype=np.float64, allow_blocked=allow_blocked)
    return RoiResult(
        names=names,
        stat=np.asarray(result.stat, dtype=np.float64),
        p_unc=np.asarray(result.p_unc, dtype=np.float64),
        p_fwe=np.asarray(result.p_fwe, dtype=np.float64),
        effect=np.asarray(result.effect, dtype=np.float64),
        effect_kind=result.effect_kind,
        kind=result.kind,
        tails=int(result.tails),
        df=int(result.df),
        n_perm=int(result.n_perm),
        min_p=float(result.min_p),
        exhaustive=bool(result.exhaustive),
        seed=int(result.seed),
        max_null=np.asarray(result.max_null),
    )


def small_volume_fwe(design, maps, roi_mask, n_perm=None, seed=None, allow_blocked=False):
    """Voxelwise GLM restricted to the voxels of one ROI (small-volume FWE).

    :param maps: n x V maps in design order.
    :param roi_mask: V-vector (bool, or weights > 0.5 inside).
    :return: (``glm.GlmResult`` over the ROI's voxels, the indices of those voxels in 0..V-1).
    """
    from . import glm

    maps = np.asarray(maps)
    mask = _as_masks(roi_mask, maps.shape[1])[0]
    indices = np.flatnonzero(mask)
    result = glm.run(design, np.asarray(maps[:, indices]), n_perm=n_perm, seed=seed, tfce=False, dtype=np.float64, allow_blocked=allow_blocked)
    return result, indices


# Files


def _same_grid(shape_a, affine_a, shape_b, affine_b):
    return tuple(shape_a[:3]) == tuple(shape_b[:3]) and np.allclose(np.asarray(affine_a), np.asarray(affine_b), atol=AFFINE_TOLERANCE)


def read_roi(path, mask_shape, mask_affine, indices):
    """An ROI NIfTI as a boolean vector over the connectome mask's voxels (``indices``: flat in-mask indices)."""
    from . import nifti

    data, affine, header = nifti.read_nifti(path)
    shape = tuple(header["shape"][:3])
    if not _same_grid(shape, affine, mask_shape, mask_affine):
        raise RoiError(f"{os.path.basename(path)} is not on the connectome grid ({'x'.join(map(str, shape))} vs {'x'.join(map(str, mask_shape))}); resample it first")
    return np.asarray(data, dtype=np.float32).reshape(shape).reshape(-1)[indices] > INSIDE  # flat C order, as geometry.mask_indices


def write_svc_mask(roi_path, mask_path, out_path):
    """Write the ROI restricted to the connectome mask, on the connectome grid (for ``glm --mask``); return its voxel count."""
    from . import nifti

    mask_data, mask_affine, mask_header = nifti.read_nifti(mask_path)
    shape = tuple(mask_header["shape"][:3])
    mask = np.asarray(mask_data).reshape(shape) != 0
    data, affine, header = nifti.read_nifti(roi_path)
    roi_shape = tuple(header["shape"][:3])
    if not _same_grid(roi_shape, affine, shape, mask_affine):
        raise RoiError(f"{os.path.basename(roi_path)} is not on the connectome grid; resample it first")
    inside = (np.asarray(data, dtype=np.float32).reshape(roi_shape) > INSIDE) & mask
    count = int(inside.sum())
    if count == 0:
        raise RoiError(f"{os.path.basename(roi_path)} has no voxel inside the connectome mask")
    from . import geometry, glm

    indices = geometry.mask_indices(mask)
    # The connectome mask's exact header (geometry, codes, byte order), as the GLM writes its outputs
    glm._write_on_mask(out_path, inside.reshape(-1)[indices].astype(np.float32), nifti.read_raw_header(mask_path), indices, shape, descrip="ROI in connectome mask (small-volume FWE)")
    return count


def _parser():
    from . import jobs

    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.roi", description="A-priori ROI inference for lesion network mapping.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=jobs.JsonArgumentParser)
    run = commands.add_parser("glm", help="permutation GLM on ROI means, FWE across ROIs")
    run.add_argument("--maps", required=True, help="maps.tsv of a cohortmaps run")
    run.add_argument("--participants", required=True, help="participants.tsv")
    run.add_argument("--design", default=None, help="design.json (default: two groups, case vs control, volume covariate)")
    run.add_argument("--mask", required=True, help="connectome mask (the maps' grid)")
    run.add_argument("--roi", action="append", required=True, help="ROI mask NIfTI on the connectome grid (repeatable)")
    run.add_argument("--perm", type=jobs.positive_int, default=None)
    run.add_argument("--seed", type=int, default=None)
    run.add_argument("--out", required=True)
    jobs.add_common_arguments(run, threads_default=None)
    svc = commands.add_parser("svc-mask", help="write the ROI restricted to the connectome mask, for glm --mask (small-volume FWE)")
    svc.add_argument("--roi", required=True)
    svc.add_argument("--mask", required=True)
    svc.add_argument("--out", required=True)
    return parser


def _run_glm(args):
    from . import design as designlib
    from . import geometry, glm, jobs, nifti
    from .export import write_json_atomic

    study, _in_study = glm._load_study(args.participants)
    try:
        spec = designlib.DesignSpec.load(args.design) if args.design else designlib.DesignSpec()
    except (OSError, ValueError) as exc:
        raise jobs.JobError(f"The design {args.design} cannot be read: {exc}", code="usage", exit_code=jobs.EXIT_USAGE) from None
    mapped = glm._map_rows(os.path.abspath(args.maps), glm.MAP_COLUMNS[spec.input])
    with_maps = {sid for sid in study.subject_ids if sid in mapped}
    if not with_maps:
        raise jobs.JobError("No subject has a map in maps.tsv", code="design_blocked", exit_code=jobs.EXIT_USAGE)
    subset, _filled = glm._subset_study(study, with_maps, {sid: values[1] for sid, values in mapped.items()})
    try:
        design = designlib.build_design(subset, spec)
    except designlib.DesignError as exc:
        raise jobs.JobError(f"The design cannot be built: {exc}", code="design_blocked", exit_code=jobs.EXIT_USAGE) from None
    if design.blocking:
        raise jobs.JobError("The design is blocked: " + " | ".join(check.message for check in design.blocking), code="design_blocked", exit_code=jobs.EXIT_USAGE)
    mask_data, mask_affine, mask_header = nifti.read_nifti(args.mask)
    shape = tuple(mask_header["shape"][:3])
    indices = geometry.mask_indices(np.asarray(mask_data).reshape(shape) != 0)
    try:
        rois = np.array([read_roi(path, shape, mask_affine, indices) for path in args.roi])
        names = [os.path.basename(path).split(".nii")[0] for path in args.roi]
        voxels = rois.sum(axis=1)
        out_dir = os.path.abspath(args.out)
        os.makedirs(out_dir, exist_ok=True)
        jobs.emit("start", job="roi", n=design.n, n_rois=len(names))
        Y = glm._load_maps([mapped[sid][0] for sid in design.sids], shape, mask_affine, indices, os.path.join(out_dir, ".roi-maps.npy"), jobs.emit)
        means = roi_means(Y, rois)
        result = roi_glm(design, means, names, n_perm=args.perm, seed=args.seed)
    except RoiError as exc:
        raise jobs.JobError(str(exc), code="roi", exit_code=jobs.EXIT_USAGE) from None
    finally:
        memmap = os.path.join(os.path.abspath(args.out), ".roi-maps.npy")
        if os.path.exists(memmap):
            os.remove(memmap)
    record = {"format": FORMAT_NAME, "version": FORMAT_VERSION, "design_type": design.design_type, "contrast": glm.contrast_label(design), "n": design.n,
              "rois_files": [os.path.basename(path) for path in args.roi], **result.to_dict(voxels)}
    write_json_atomic(os.path.join(out_dir, ROI_GLM_JSON), record)
    with open(os.path.join(out_dir, ROI_MEANS_TSV), "w", encoding="utf-8", newline="\n") as f:
        f.write("\t".join(["participant_id", *names]) + "\n")
        for sid, row in zip(design.sids, means, strict=True):
            f.write("\t".join([sid, *(f"{value:.6g}" for value in row)]) + "\n")
    jobs.emit("done", outputs=[ROI_GLM_JSON, ROI_MEANS_TSV], rois=record["rois"])
    return 0


def main(argv=None):
    from . import jobs

    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.command == "svc-mask":
        try:
            count = write_svc_mask(args.roi, args.mask, args.out)
        except RoiError as exc:
            print(json.dumps({"event": "error", "message": str(exc)}))
            return jobs.EXIT_USAGE
        print(json.dumps({"event": "done", "out": os.path.abspath(args.out), "voxels": count, "next": "python -m LesionNetworkMappingLib.glm --mask <out> ... (small-volume FWE)"}))
        return 0
    return _run_glm(args)


if __name__ == "__main__":
    from LesionNetworkMappingLib import jobs as _jobs

    _jobs.run_job(main, job="roi")

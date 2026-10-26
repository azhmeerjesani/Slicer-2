"""PALM round-trip: export a design and its maps for PALM (and FSL randomise), validate the export,
and import PALM results (numpy only; no Slicer imports).

PALM (Winkler et al. 2014, https://github.com/andersonwinkler/PALM) is GPL software and is never
vendored or run by this library: :func:`export_palm` writes the files and a ``run_palm.sh`` whose
command reproduces the in-app design exactly (SPEC_LNM_ADDENDUM §3.4):

- the same design matrix, contrast and subject order (``design.csv``, ``contrasts.csv``, ``subjects.tsv``);
- ``-twotail`` when the in-app test is two-sided;
- ``-eb eb.csv`` with ``-within`` or ``-whole`` set explicitly when permutations use blocks, ``-ise``
  (sign flipping) for one-sample designs;
- ``-vg vg.csv`` (the groups as variance groups) only when the Aspin-Welch v statistic was chosen;
- ``-T`` with the in-app TFCE parameters (H = 2, E = 0.5, 26-connectivity) when TFCE is on.

The same matrices are also written in FSL VEST format (``design.mat``, ``design.con``, ``design.fts``,
``design.grp``) for ``run_randomise.sh``. In VEST, ``design.grp`` holds exchangeability blocks for
randomise (``-e``), not PALM variance groups. :func:`validate_palm_export` re-reads the files and runs
the same design checks as the in-app builder (``design.matrix_checks``); :func:`import_palm` reads
PALM's outputs back (``-logp`` detected from ``<prefix>_palmconfig.txt`` or the value range).
"""

import gzip
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import struct
import tempfile

import numpy as np

from . import design as designlib
from . import nifti, schema
from .geometry import from_masked_vector, mask_indices

EXPORT_FORMAT = "slicer-lnm-palm-export"
EXPORT_VERSION = 1
EXPORT_JSON = "palm_export.json"
DATA_FILE = "fz_4d.nii.gz"
MASK_FILE = "mask.nii.gz"
DESIGN_CSV = "design.csv"
CONTRASTS_CSV = "contrasts.csv"
FTESTS_CSV = "ftests.csv"
EB_CSV = "eb.csv"
VG_CSV = "vg.csv"
SUBJECTS_TSV = "subjects.tsv"
VEST_DESIGN = "design.mat"
VEST_CONTRASTS = "design.con"
VEST_FTESTS = "design.fts"
VEST_GROUPS = "design.grp"
PALM_SCRIPT = "run_palm.sh"
RANDOMISE_SCRIPT = "run_randomise.sh"
README = "README.md"
OUTPUT_PREFIX = "out/palm"
#: TFCE parameters of the in-app engine (glm/tfce, SPEC_LNM_ADDENDUM §3.4)
TFCE_H = 2
TFCE_E = 0.5
TFCE_C = 26
PALM_URL = "https://github.com/andersonwinkler/PALM"
_DESIGN_KEYS = ("test", "group_column", "levels", "contrast", "score_column", "covariates", "transforms", "block_column", "permute", "input", "variance_groups", "restrict_to_mixed_blocks")
_OUTPUT = re.compile(r"^(?P<kind>vox|tfce|clustere|clusterm|dpv)_(?P<stat>[a-z]+stat)(?:_(?P<ptype>c?fwep|c?fdrp|uncp))?(?P<rest>(?:_[mdc]\d+)*)\.nii(?:\.gz)?$")
_P_TYPES = ("fwep", "cfwep", "fdrp", "cfdrp", "uncp")


# Export


def _number(value):
    value = float(value)
    return "%.10g" % (0.0 if value == 0 else value)


def _csv_text(matrix):
    matrix = np.atleast_2d(np.asarray(matrix, dtype=np.float64))
    return "".join(",".join(_number(value) for value in row) + "\n" for row in matrix)


def _column_text(values):
    return "".join(f"{int(value)}\n" for value in values)


def _vest_text(matrix, kind="mat", names=None):
    """FSL VEST text: /NumWaves, /NumPoints (/NumContrasts for .con and .fts), /PPheights, /Matrix."""
    matrix = np.atleast_2d(np.asarray(matrix, dtype=np.float64))
    lines = []
    if kind == "con":
        for index, name in enumerate(names or [], 1):
            lines.append(f"/ContrastName{index}\t{name}")
        lines += [f"/NumWaves\t{matrix.shape[1]}", f"/NumContrasts\t{matrix.shape[0]}"]
    elif kind == "fts":
        # As FEAT writes design.fts: one wave per t contrast, one row per F test
        lines += [f"/NumWaves\t{matrix.shape[1]}", f"/NumContrasts\t{matrix.shape[0]}"]
    else:
        lines += [f"/NumWaves\t{matrix.shape[1]}", f"/NumPoints\t{matrix.shape[0]}"]
    if kind in ("mat", "con"):
        heights = np.ptp(matrix, axis=0) if kind == "mat" else np.abs(matrix).max(axis=1)
        lines.append("/PPheights\t" + "\t".join(_number(value) for value in heights))
    lines += ["", "/Matrix"]
    lines += ["\t".join(_number(value) for value in row) for row in matrix]
    return "\n".join(lines) + "\n"


def _write(folder, name, text, mode=None):
    path = os.path.join(folder, name)
    schema.write_text_atomic(path, text)
    if mode is not None:
        os.chmod(path, mode)
    return path


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _write_4d(path, maps, indices, shape, affine, header, descrip):
    """Write n masked maps as one 4D float32 NIfTI on the mask grid, one volume at a time.

    The header comes from ``nifti.write_nifti`` (the same sform, qform and codes as every other map of
    the library) for a single volume; only dim[4] is changed, so memory stays at one volume.
    """
    folder = os.path.dirname(os.path.abspath(path))
    scratch = tempfile.mkdtemp(prefix=".palm-header-", dir=folder)
    try:
        template = os.path.join(scratch, "header.nii")
        nifti.write_nifti(template, np.zeros((*shape, 1), dtype=np.float32), affine, sform_code=header["sform_code"], qform_code=header["qform_code"], descrip=descrip)
        with open(template, "rb") as f:
            head = bytearray(f.read(nifti.HEADER_SIZE + 4))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    struct.pack_into("<8h", head, 40, 4, *shape, len(maps), 1, 1, 1)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=1, mtime=0) as f:
            f.write(bytes(head))
            for row in maps:
                volume = from_masked_vector(np.asarray(row, dtype=np.float32), indices, shape, dtype=np.float32)
                f.write(volume.astype("<f4", copy=False).tobytes(order="F"))
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def palm_command(design, inference=None):
    """The PALM command (a list of arguments) that reproduces a design, run from the export folder."""
    inference = {**design.spec.inference, **(inference or {})}
    args = ["palm", "-i", DATA_FILE, "-m", MASK_FILE, "-d", DESIGN_CSV, "-t", CONTRASTS_CSV]
    if design.stat == "F":
        args += ["-f", FTESTS_CSV, "-fonly"]
    if design.blocks is not None:
        args += ["-eb", EB_CSV, "-whole" if design.permute == "whole" else "-within"]
    if design.exchange == "sign_flip":
        args.append("-ise")
    args += ["-n", str(int(inference["n_perm"]))]
    if inference["tfce"]:
        args += ["-T", "-tfce_H", str(TFCE_H), "-tfce_E", str(TFCE_E), "-tfce_C", str(TFCE_C)]
    args += ["-logp", "-corrcon"]
    if inference["fdr"]:
        args.append("-fdr")
    if design.tails == 2:
        args.append("-twotail")
    if design.variance_groups is not None:
        args += ["-vg", VG_CSV]
    if int(inference["seed"]) > 0:
        args += ["-seed", str(int(inference["seed"]))]
    args += ["-o", OUTPUT_PREFIX]
    return args


def randomise_command(design, inference=None):
    """The FSL randomise command (a list of arguments) closest to a design (see the README for its limits)."""
    inference = {**design.spec.inference, **(inference or {})}
    args = ["randomise", "-i", DATA_FILE, "-o", "out/randomise", "-m", MASK_FILE]
    if design.exchange == "sign_flip" and design.p == 1:
        args.append("-1")
    else:
        args += ["-d", VEST_DESIGN, "-t", VEST_CONTRASTS]
        if design.stat == "F":
            args += ["-f", VEST_FTESTS, "--fonly"]
    if design.blocks is not None:
        args += ["-e", VEST_GROUPS]
        if design.permute == "whole":
            args.append("--permuteBlocks")
    args += ["-n", str(int(inference["n_perm"]))]
    if inference["tfce"]:
        args += ["-T", f"--tfce_H={TFCE_H}", f"--tfce_E={TFCE_E}", f"--tfce_C={TFCE_C}"]
    args += ["-x", "--uncorrp"]
    if int(inference["seed"]) > 0:
        args.append(f"--seed={int(inference['seed'])}")
    return args


def _script(args, title):
    return "\n".join(
        [
            "#!/bin/sh",
            f"# {title}",
            "# Generated by Slicer Studio (LesionNetworkMappingLib.palm); run it from any folder.",
            "set -e",
            'cd "$(dirname "$0")"',
            "mkdir -p out",
            shlex.join(args),
            "",
        ],
    )


def _randomise_contrasts(design):
    """(contrast rows, names) for design.con: randomise is one-tailed, so a two-sided test lists the contrast and its negation."""
    rows, names = [design.C[index] for index in range(design.C.shape[0])], list(design.contrast_names)
    if design.tails == 2 and design.stat != "F":
        rows += [-row for row in rows]
        names += [f"-{name}" for name in names]
    return np.array(rows), names


def _readme(design, inference, palm_args, randomise_args, mask_path, files):
    spec = design.spec
    contrast = "; ".join(f"{name}: [{', '.join(_number(value) for value in row)}]" for name, row in zip(design.contrast_names, design.C, strict=True))
    if design.exchange == "sign_flip":
        scheme = "`-ise`" + (" `-eb eb.csv -whole`" if design.blocks is not None else "")
    elif design.blocks is not None:
        scheme = f"`-eb eb.csv -{design.permute}`"
    else:
        scheme = "no `-eb`"
    lines = [
        "# PALM export",
        "",
        _disclaimer(),
        "",
        "This folder reproduces a Slicer Studio group analysis in PALM (Winkler et al. 2014). PALM is GPL software and is",
        f"not included: get it from {PALM_URL} and run `sh {PALM_SCRIPT}`. Outputs go to `out/` and can be read back",
        "with Map > Contrast > Import PALM results.",
        "",
        "## Design",
        "",
        f"- Test: {spec.test}, design type {design.design_type}; statistic {design.stat}; {'two' if design.tails == 2 else 'one'}-tailed.",
        f"- Subjects: {design.n} (volume order in `{SUBJECTS_TSV}`); input maps: {spec.input} (`{DATA_FILE}`, float32, on the grid of `{os.path.basename(mask_path)}`).",
        f"- Columns of `{DESIGN_CSV}` / `{VEST_DESIGN}`: {', '.join(design.columns)}.",
        f"- Contrast (`{CONTRASTS_CSV}`): {contrast}.",
        f"- Covariates: {', '.join(spec.covariates) or 'none'}" + (f" (lesion volume off: {spec.volume_off_reason})" if spec.volume_off_reason else "") + ".",
        f"- Exchangeability: {design.permute_reason} (PALM: {scheme}).",
        f"- Permutations: {int(inference['n_perm'])} requested; {design.unique_perms if design.unique_perms < 10**15 else 'more than 1e15'} unique"
        + (" (all are enumerated: exhaustive)" if design.exhaustive else "")
        + f"; smallest attainable p {design.min_p:.3g}.",
    ]
    if design.variance_groups is not None:
        lines.append(f"- Variance groups (`{VG_CSV}`, PALM `-vg`): the levels of {spec.group_column}, for the Aspin-Welch v statistic.")
    if inference["tfce"]:
        lines.append(f"- TFCE: H = {TFCE_H}, E = {TFCE_E}, {TFCE_C}-connectivity.")
    if design.limitations:
        lines += ["", "## Limitations", ""] + [f"- {text}" for text in design.limitations]
    warnings = [check for check in design.checks if check.level in ("block", "warn")]
    if warnings:
        lines += ["", "## Design checks", ""] + [f"- {check.level}: {check.message}" for check in warnings]
    lines += [
        "",
        "## Commands",
        "",
        "```sh",
        shlex.join(palm_args),
        "```",
        "",
        "- `-twotail` is present exactly when the in-app test is two-sided; `-eb` comes with `-within` or `-whole` set explicitly.",
        "- `-vg` is used only when the v statistic was chosen in the app.",
        "- `-logp` writes p-values as -log10(p); `-corrcon` also corrects across contrasts; `-fdr` adds FDR-adjusted p-values.",
        "- PALM draws its own random permutations, so p-values differ from the app's by Monte Carlo error unless the enumeration is exhaustive.",
        "",
        "```sh",
        shlex.join(randomise_args),
        "```",
        "",
        "FSL randomise reads the VEST files. Note:",
        "",
        f"- **`{VEST_GROUPS}` holds exchangeability blocks for randomise (`-e`), not PALM variance groups.** PALM reads blocks from",
        f"  `{EB_CSV}` (`-eb`) and variance groups from `{VG_CSV}` (`-vg`). Without blocks `{VEST_GROUPS}` is all ones, as FEAT writes it.",
        "- randomise tests each contrast one-tailed: for a two-sided test `design.con` lists the contrast and its negation;",
        "  combine them as p = min(1, 2 min(p+, p-)).",
        "- randomise has no variance groups (v statistic), no FDR and no -log10(p) output.",
        "",
        "## Files",
        "",
    ]
    lines += [f"- `{name}`" for name in files]
    return "\n".join(lines) + "\n"


def _disclaimer():
    from .export import DISCLAIMER

    return DISCLAIMER


def _check_spec(design, spec):
    """The inference settings to export: the design's, or those of ``spec`` when its design keys match."""
    if spec is None:
        return dict(design.spec.inference)
    spec = designlib.as_spec(spec)
    mine, theirs = design.spec.to_dict(), spec.to_dict()
    different = [key for key in _DESIGN_KEYS if mine.get(key) != theirs.get(key)]
    if different:
        raise ValueError(f"The spec differs from the design's spec in {', '.join(different)}; rebuild the design")
    return dict(spec.inference)


def export_palm(design, maps, mask_path, out_dir, spec=None, allow_blocked=False):
    """Write a PALM package that reproduces a design: the 4D maps, mask, design and contrast files,
    exchangeability blocks, variance groups, VEST files, run_palm.sh, run_randomise.sh and README.md.

    :param design: ``design.Design`` (from ``design.build_design``).
    :param maps: n x V float maps in design order (``design.sids``); V is the number of mask voxels,
      in C order of the [i, j, k] mask (``geometry.mask_indices``), as in the connectome bundle.
    :param mask_path: the connectome mask NIfTI; the 4D image gets its exact grid and header.
    :param out_dir: the export folder (created; e.g. ``derivatives/lnm/palm/<run>``).
    :param spec: optional spec whose inference settings (n_perm, seed, TFCE, FDR) are exported; its
      design keys must equal the design's.
    :param allow_blocked: export even when design checks block the analysis.
    :return: {name: path} of the written files.
    :raises ValueError: blocking design checks, maps of the wrong shape or with non-finite values.
    """
    inference = _check_spec(design, spec)
    if design.blocking and not allow_blocked:
        raise ValueError("The design has blocking checks: " + "; ".join(check.message for check in design.blocking))
    maps = np.asarray(maps)
    data, affine, header = nifti.read_nifti(mask_path)
    mask = np.asarray(data).reshape(header["shape"][:3]) != 0
    indices = mask_indices(mask)
    if maps.ndim != 2 or maps.shape != (design.n, len(indices)):
        raise ValueError(f"Expected maps of shape ({design.n}, {len(indices)}) (subjects in design order, mask voxels), got {maps.shape}")
    finite = np.isfinite(maps).all(axis=1)
    if not finite.all():
        bad = [sid for sid, ok in zip(design.sids, finite, strict=True) if not ok]
        raise ValueError(f"Maps with non-finite values: {', '.join(bad[:10])}")
    out_dir = os.fspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(os.path.join(out_dir, "out"), exist_ok=True)
    paths = {}
    paths[DATA_FILE] = os.path.join(out_dir, DATA_FILE)
    _write_4d(paths[DATA_FILE], maps, indices, mask.shape, affine, header, f"LNM {design.spec.input} maps N={design.n} (PALM export)")
    paths[MASK_FILE] = os.path.join(out_dir, MASK_FILE)
    nifti.write_nifti(paths[MASK_FILE], mask.astype(np.uint8), affine, sform_code=header["sform_code"], qform_code=header["qform_code"], descrip="LNM connectome mask")

    paths[DESIGN_CSV] = _write(out_dir, DESIGN_CSV, _csv_text(design.X))
    paths[CONTRASTS_CSV] = _write(out_dir, CONTRASTS_CSV, _csv_text(design.C))
    if design.stat == "F":
        paths[FTESTS_CSV] = _write(out_dir, FTESTS_CSV, _csv_text(np.ones((1, design.C.shape[0]))))
    if design.blocks is not None:
        paths[EB_CSV] = _write(out_dir, EB_CSV, _column_text(design.blocks))
    if design.variance_groups is not None:
        paths[VG_CSV] = _write(out_dir, VG_CSV, _column_text(design.variance_groups))
    paths[VEST_DESIGN] = _write(out_dir, VEST_DESIGN, _vest_text(design.X))
    rows, names = _randomise_contrasts(design)
    paths[VEST_CONTRASTS] = _write(out_dir, VEST_CONTRASTS, _vest_text(rows, "con", names))
    if design.stat == "F":
        paths[VEST_FTESTS] = _write(out_dir, VEST_FTESTS, _vest_text(np.ones((1, design.C.shape[0])), "fts"))
    groups = design.blocks if design.blocks is not None else np.ones(design.n, dtype=np.int64)
    paths[VEST_GROUPS] = _write(out_dir, VEST_GROUPS, _vest_text(np.asarray(groups).reshape(-1, 1), "grp"))
    tested = design.group_labels if design.group_labels is not None else [None] * design.n
    lines = ["volume\tparticipant_id\tlevel\tblock"]
    for index, sid in enumerate(design.sids):
        block = design.block_labels[index] if design.block_labels is not None else schema.NA
        lines.append(f"{index + 1}\t{sid}\t{tested[index] if tested[index] is not None else schema.NA}\t{block}")
    paths[SUBJECTS_TSV] = _write(out_dir, SUBJECTS_TSV, "\n".join(lines) + "\n")
    design_json, design_tsv = designlib.save_design(design, out_dir)
    paths[designlib.DESIGN_JSON], paths[designlib.DESIGN_MATRIX_TSV] = design_json, design_tsv

    palm_args, randomise_args = palm_command(design, inference), randomise_command(design, inference)
    paths[PALM_SCRIPT] = _write(out_dir, PALM_SCRIPT, _script(palm_args, "PALM command reproducing the in-app design"), 0o755)
    paths[RANDOMISE_SCRIPT] = _write(out_dir, RANDOMISE_SCRIPT, _script(randomise_args, "FSL randomise command closest to the in-app design (see README.md)"), 0o755)
    names = [*paths, README, EXPORT_JSON]
    paths[README] = _write(out_dir, README, _readme(design, inference, palm_args, randomise_args, mask_path, names))
    manifest = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "n": design.n,
        "p": design.p,
        "columns": list(design.columns),
        "contrast_names": list(design.contrast_names),
        "stat": design.stat,
        "tails": design.tails,
        "design_type": design.design_type,
        "permute": design.permute,
        "exchange": design.exchange,
        "block_column": design.block_column,
        "blocks_used": design.blocks is not None,
        "n_perm": int(inference["n_perm"]),
        "seed": int(inference["seed"]),
        "tfce": bool(inference["tfce"]),
        "fdr": bool(inference["fdr"]),
        "input": design.spec.input,
        "unique_perms_log10": math.log10(design.unique_perms) if design.unique_perms > 0 else None,
        "exhaustive": bool(design.exhaustive),
        "limitations": list(design.limitations),
        "palm_command": shlex.join(palm_args),
        "randomise_command": shlex.join(randomise_args),
        "mask_source": os.path.abspath(os.fspath(mask_path)),
        "files": {name: _sha256(path) for name, path in paths.items()},
    }
    paths[EXPORT_JSON] = _write(out_dir, EXPORT_JSON, json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return paths


# Validation


def read_csv_matrix(path):
    """A numeric CSV matrix without header (PALM's design, contrast and block files) as a 2D float array."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append([float(value) for value in re.split(r"[,\s]+", line)])
    if not rows or len({len(row) for row in rows}) != 1:
        raise ValueError(f"{path} is not a rectangular numeric matrix")
    return np.array(rows, dtype=np.float64)


def read_vest(path):
    """(matrix, {header key: text}) of an FSL VEST file (.mat, .con, .fts, .grp)."""
    header, rows, in_matrix = {}, [], False
    with open(path, encoding="utf-8") as f:
        for line in f:
            text = line.strip()
            if not text:
                continue
            if text.startswith("/Matrix"):
                in_matrix = True
                continue
            if not in_matrix and text.startswith("/"):
                key, _, value = text.partition("\t") if "\t" in text else text.partition(" ")
                header[key[1:]] = value.strip()
                continue
            if in_matrix:
                rows.append([float(value) for value in text.split()])
    if not rows:
        raise ValueError(f"{path} has no /Matrix values")
    matrix = np.array(rows, dtype=np.float64)
    for key, size in (("NumWaves", matrix.shape[1]), ("NumPoints", matrix.shape[0]), ("NumContrasts", matrix.shape[0])):
        if key in header and int(float(header[key])) != size:
            raise ValueError(f"{path}: /{key} is {header[key]} but the matrix has {size}")
    return matrix, header


def _command_tokens(path, program):
    """Arguments of the first line of a shell script that runs ``program`` (without the program name), or None."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            tokens = shlex.split(line, comments=True)
            if tokens and tokens[0] == program:
                return tokens[1:]
    return None


def validate_palm_export(folder):
    """Re-read an export folder and run the design checks on what PALM and randomise will see.

    Checks: the in-app matrix checks (``design.matrix_checks``: rank, intercept, estimability,
    one-sample) on design.csv and contrasts.csv; the VEST files equal the CSV files; the 4D image holds
    one volume per design row on the mask grid; run_palm.sh has -twotail, -eb with -within/-whole,
    -ise, -f/-fonly, -vg, -T, -fdr and -n exactly as recorded in palm_export.json; enough unique permutations; no single-subject block.

    :return: list of ``design.Check`` (blocking first); no "block" check means the export is valid.
    """
    folder = os.fspath(folder)
    Check = designlib.Check
    manifest = {}
    manifest_path = os.path.join(folder, EXPORT_JSON)
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    try:
        X = read_csv_matrix(os.path.join(folder, DESIGN_CSV))
        C = read_csv_matrix(os.path.join(folder, CONTRASTS_CSV))
    except (OSError, ValueError) as exc:
        return [Check("block", "export_unreadable", f"The design files cannot be read: {exc}")]
    columns = manifest.get("columns") if len(manifest.get("columns") or []) == X.shape[1] else None
    checks = list(designlib.matrix_checks(X, C, columns))
    n = X.shape[0]

    def mismatch(message):
        checks.append(Check("block", "export_mismatch", message))

    tokens = _command_tokens(os.path.join(folder, PALM_SCRIPT), "palm")
    if tokens is None:
        mismatch(f"{PALM_SCRIPT} has no palm command")
        tokens = []
    f_test = "-f" in tokens

    # VEST files must hold the same matrices
    try:
        mat, _ = read_vest(os.path.join(folder, VEST_DESIGN))
        if mat.shape != X.shape or not np.allclose(mat, X, rtol=1e-9, atol=1e-12):
            mismatch(f"{VEST_DESIGN} differs from {DESIGN_CSV}")
        con, _ = read_vest(os.path.join(folder, VEST_CONTRASTS))
        expected = np.vstack([C, -C]) if "-twotail" in tokens and not f_test else C
        if con.shape != expected.shape or not np.allclose(con, expected):
            mismatch(f"{VEST_CONTRASTS} differs from {CONTRASTS_CSV}" + (" and its negation (two-sided)" if expected is not C else ""))
        grp, _ = read_vest(os.path.join(folder, VEST_GROUPS))
        if grp.shape[0] != n:
            mismatch(f"{VEST_GROUPS} has {grp.shape[0]} rows, the design {n}")
    except (OSError, ValueError) as exc:
        mismatch(f"The VEST files cannot be read: {exc}")
        grp = None

    # Exchangeability blocks and variance groups
    blocks = None
    eb_path = os.path.join(folder, EB_CSV)
    if "-eb" in tokens:
        try:
            eb = read_csv_matrix(eb_path)
        except (OSError, ValueError) as exc:
            mismatch(f"{EB_CSV} cannot be read: {exc}")
            eb = None
        if eb is not None:
            if eb.shape != (n, 1):
                mismatch(f"{EB_CSV} must be one column of {n} block codes, got shape {eb.shape}")
            else:
                blocks = eb[:, 0].astype(np.int64)
                if grp is not None and grp.shape == (n, 1) and not np.array_equal(grp[:, 0].astype(np.int64), blocks):
                    mismatch(f"{VEST_GROUPS} (randomise -e) differs from {EB_CSV}")
        if "-within" not in tokens and "-whole" not in tokens:
            mismatch("-eb is used without -within or -whole")
    elif os.path.isfile(eb_path):
        mismatch(f"{EB_CSV} exists but {PALM_SCRIPT} does not use it (-eb)")
    if "-vg" in tokens:
        try:
            vg = read_csv_matrix(os.path.join(folder, VG_CSV))
            if vg.shape != (n, 1):
                mismatch(f"{VG_CSV} must be one column of {n} variance-group codes, got shape {vg.shape}")
        except (OSError, ValueError) as exc:
            mismatch(f"{VG_CSV} cannot be read: {exc}")
    if manifest:
        expected_flags = {
            "-twotail": manifest.get("tails") == 2,
            "-eb": bool(manifest.get("blocks_used")),
            "-vg": manifest.get("stat") == "v",
            "-within": bool(manifest.get("blocks_used")) and manifest.get("permute") != "whole",
            "-whole": bool(manifest.get("blocks_used")) and manifest.get("permute") == "whole",
            "-ise": manifest.get("exchange") == "sign_flip",
            "-f": manifest.get("stat") == "F",
            "-fonly": manifest.get("stat") == "F",
            "-T": bool(manifest.get("tfce")),
            "-fdr": bool(manifest.get("fdr", True)),
        }
        for flag, wanted in expected_flags.items():
            if (flag in tokens) != wanted:
                mismatch(f"{PALM_SCRIPT} {'lacks' if wanted else 'has'} {flag}, which the in-app design {'uses' if wanted else 'does not use'}")
        if manifest.get("n_perm") is not None:
            n_perm = tokens[tokens.index("-n") + 1] if "-n" in tokens and tokens.index("-n") + 1 < len(tokens) else None
            if n_perm != str(manifest["n_perm"]):
                mismatch(f"{PALM_SCRIPT} has -n {n_perm}, the in-app design {manifest['n_perm']} permutations")
        if manifest.get("n") not in (None, n):
            mismatch(f"palm_export.json records {manifest.get('n')} subjects, {DESIGN_CSV} has {n}")

    # 4D maps on the mask grid
    try:
        data_header = nifti.read_nifti_header(os.path.join(folder, DATA_FILE))
        mask_header = nifti.read_nifti_header(os.path.join(folder, MASK_FILE))
        volumes = data_header["dim"][4] if data_header["dim"][0] >= 4 else 1
        if volumes != n:
            mismatch(f"{DATA_FILE} has {volumes} volumes, the design {n} rows")
        if tuple(data_header["shape"][:3]) != tuple(mask_header["shape"][:3]) or not np.allclose(data_header["affine"], mask_header["affine"], atol=1e-5):
            mismatch(f"{DATA_FILE} is not on the grid of {MASK_FILE}")
    except (OSError, ValueError) as exc:
        mismatch(f"The images cannot be read: {exc}")

    # Permutations
    labels = designlib._row_codes(X)
    if "-ise" in tokens and "-ee" not in tokens:
        units = len(set(blocks.tolist())) if blocks is not None and "-whole" in tokens else n
        unique, what = 2**units, "sign flips"
    else:
        mode = "whole" if blocks is not None and "-whole" in tokens else "within" if blocks is not None else "free"
        try:
            unique = designlib.unique_count(n, blocks, labels, mode=mode)
        except ValueError as exc:
            mismatch(str(exc))
            unique = 0
        what = "permutations"
    if unique < designlib.MIN_UNIQUE_PERMS:
        checks.append(Check("block", "too_few_permutations", f"Only {unique} unique {what} exist (at least {designlib.MIN_UNIQUE_PERMS} are needed)", {"unique_perms": int(unique)}))
    if blocks is not None:
        counts = np.bincount(blocks - blocks.min())
        if np.any(counts == 1):
            checks.append(Check("block", "singleton_block", f"{EB_CSV} has a block with a single subject"))
    return designlib._ordered(checks)


# Import


def _palm_config(folder, prefix):
    """(arguments, path) of PALM's <prefix>_palmconfig.txt, or (None, None)."""
    path = os.path.join(folder, f"{prefix}_palmconfig.txt")
    if not os.path.isfile(path):
        return None, None
    tokens = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.lstrip().startswith("#"):
                continue
            tokens += shlex.split(line, comments=True)
    if tokens and tokens[0] == "palm":
        tokens = tokens[1:]
    return tokens, path


def import_palm(folder, prefix="palm", mask_path=None):
    """Read PALM outputs (``<prefix>_<vox|tfce>_<stat>[_<fwep|cfwep|fdrp|cfdrp|uncp>][_m<k>][_d<k>][_c<k>].nii[.gz]``).

    Looks in ``folder`` and ``folder/out`` (where run_palm.sh writes). P-values are returned as p:
    -log10(p) files are converted when ``-logp`` is in ``<prefix>_palmconfig.txt`` or, without that
    file, when any p-value file has values above 1.

    :param mask_path: optional mask: arrays are then the in-mask values in C order (the voxel order of
      the library's maps); otherwise they are volumes indexed [i, j, k].
    :return: {"folder", "prefix", "logp", "logp_source" ("config" or "range"), "command" (PALM's
      recorded command line, or None), "config", "exported_command" (from run_palm.sh, or None),
      "contrasts": {k: {name: array}}, "maps": the arrays of the first contrast, "files": {k: {name: path}},
      "stat": the statistic name (tstat, vstat, fstat, ...), "affine"}. Names are "vox_stat",
      "vox_fwep", "vox_fdrp", "vox_uncp", "vox_cfwep", "tfce_stat", "tfce_fwep", ... ("_m<k>"/"_d<k>"
      are appended for modalities or designs other than 1).
    :raises FileNotFoundError: no output with this prefix.
    """
    folder = os.fspath(folder)
    found = {}
    out_folder = None
    for candidate in (folder, os.path.join(folder, "out")):
        if not os.path.isdir(candidate):
            continue
        start = f"{prefix}_"
        for name in sorted(os.listdir(candidate)):
            match = _OUTPUT.match(name[len(start) :]) if name.startswith(start) else None
            if match is None:
                continue
            extras = dict(re.findall(r"_([mdc])(\d+)", match.group("rest")))
            key = f"{match.group('kind')}_{match.group('ptype') or 'stat'}"
            for letter in ("m", "d"):
                if int(extras.get(letter, 1)) != 1:
                    key += f"_{letter}{extras[letter]}"
            contrast = int(extras.get("c", 1))
            found.setdefault(contrast, {})[key] = (os.path.join(candidate, name), match.group("stat"))
            out_folder = out_folder or candidate
    if not found:
        raise FileNotFoundError(f"No PALM outputs named {prefix}_* in {folder} or {os.path.join(folder, 'out')}")
    tokens, config = _palm_config(out_folder, prefix)
    indices, shape = None, None
    if mask_path is not None:
        data, _, header = nifti.read_nifti(mask_path)
        mask = np.asarray(data).reshape(header["shape"][:3]) != 0
        indices, shape = mask_indices(mask), mask.shape
    arrays, files, stat, affine = {}, {}, None, None
    for contrast, entries in sorted(found.items()):
        arrays[contrast], files[contrast] = {}, {}
        for key, (path, stat_name) in entries.items():
            data, file_affine, header = nifti.read_nifti(path)
            data = np.asarray(data, dtype=np.float64).reshape(header["shape"][:3])
            if indices is not None:
                if data.shape != shape:
                    raise ValueError(f"{path} has shape {data.shape}, the mask {shape}")
                data = data.reshape(-1)[indices]
            arrays[contrast][key] = data
            files[contrast][key] = path
            stat = stat or stat_name
            affine = file_affine if affine is None else affine
    p_keys = [(contrast, key) for contrast, entries in arrays.items() for key in entries if key.split("_")[1] in _P_TYPES]
    if tokens is not None:
        logp, source = "-logp" in tokens, "config"
    else:
        logp = any(float(np.nanmax(arrays[contrast][key])) > 1.0 + 1e-6 for contrast, key in p_keys if arrays[contrast][key].size)
        source = "range"
    if logp:
        for contrast, key in p_keys:
            arrays[contrast][key] = np.power(10.0, -arrays[contrast][key])
    exported = None
    for candidate in (folder, os.path.dirname(os.path.abspath(folder))):
        args = _command_tokens(os.path.join(candidate, PALM_SCRIPT), "palm")
        if args is not None:
            exported = shlex.join(["palm", *args])
            break
    first = min(arrays)
    return {
        "folder": out_folder,
        "prefix": prefix,
        "logp": bool(logp),
        "logp_source": source,
        "command": shlex.join(["palm", *tokens]) if tokens else None,
        "config": config,
        "exported_command": exported,
        "contrasts": arrays,
        "maps": arrays[first],
        "files": files,
        "stat": stat,
        "affine": affine,
    }

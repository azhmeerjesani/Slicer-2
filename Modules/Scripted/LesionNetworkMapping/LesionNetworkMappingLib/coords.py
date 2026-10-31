"""Coordinate, atrophy and stimulation-site seeds (SPEC_LNM_ADDENDUM F36; research methods §5-6).

numpy and the standard library only; no Slicer imports. Every seed type becomes ordinary seed NIfTIs on the
connectome grid, mapped by ``cohortmaps`` like lesions, so coordinate network mapping (CNM; Darby 2019), atrophy
network mapping (Tetreault 2020) and stimulation-site mapping (Horn 2017; Siddiqi 2020) use the same engine and the
same statistics as lesions.

**Coordinates.**

- Parsers: :func:`read_csv` (``x, y, z[, radius, study, space]``, with or without a header; a ``diameter`` column is
  read as a diameter), :func:`read_sleuth` (BrainMap Sleuth/GingerALE text: ``// Reference=MNI|Talairach`` and
  experiment blocks) and :func:`read_nimare_json` (a NiMARE dataset JSON); :func:`read_foci` picks by content.
- Talairach to MNI: :func:`tal2mni` applies the inverse of the published Lancaster et al. (2007) icbm2tal
  transform (Hum Brain Mapp 28:1194, doi:10.1002/hbm.20345), reimplemented as in NiMARE ``utils.tal2mni`` (MIT
  licence), which uses the "pooled" (other) matrix; the SPM and FSL matrices of the same paper are available by name.
  :func:`to_mni` records what was converted (:data:`LANCASTER_CITATION`, the matrix) for provenance and methods.
- :func:`spheres`: the union of spheres around foci as fractional occupancy of each grid voxel, computed through the
  grid's affine (regular sub-samples of each voxel). Exactly one of ``radius_mm`` and ``diameter_mm`` is given
  (4 mm radius when neither is: Darby 2019; Ji 2025 used a 6 mm *diameter*); the record always states both.
- :func:`study_union_seeds`: one study-level union seed per study, as the columns of one weight matrix (one engine
  pass for all studies).
- :func:`random_coordinate_null`: the random-coordinate control (Ji 2025; Wall 2025): ``k`` null sets of studies
  with the same number of foci per study, drawn uniformly among gray-matter voxels (GM prior probability > 0.5, of
  the same template family as the grid: :func:`gm_mask_for` refuses another family), mapped like the observed
  studies; returns the observed overlap, the null overlap maps (optional), the null distribution of the peak
  overlap and the observed percentile, and the voxelwise exceedance p.

**Atrophy.** :func:`atrophy_seed`: a per-patient w-map (or z-map) thresholded with ``w < -2`` (default), ``-1.5``,
``-2.5`` or the most atrophied 5 % (``top5%``), as a binary or weighted seed (Tetreault 2020).

**Stimulation.** :func:`stimulation_seed`: points with a radius (binary or graded sphere), a SimNIBS E-field NIfTI
(normalized, or thresholded at a field strength) or a Lead-DBS VTA NIfTI (binary or probabilistic), as a seed on the
grid. :func:`outcome_rmap` and :func:`outcome_weighted_map` are the in-memory R-map and outcome-weighted map
(Horn 2017); :func:`slnm_arguments` gives the permutation sLNM (``slnm.py``, continuous score on Fz, ``--flip`` for
TMS cohorts where a larger anticorrelation means a better response).

**Run folder.** :func:`write_seed_set` writes ``seeds/<id>_seed.nii.gz`` (float32 weights, the grid's exact
header), ``lesions.tsv`` (participant_id, lesion relative to the folder, lesion_sha256) and ``seeds.json`` (kind,
sphere radius and diameter, space conversion, sources by file name and sha256: never an absolute path, SPEC 9.7
rule 10). :func:`cohortmaps_arguments` gives the ``cohortmaps`` command line that maps them (``--probabilistic``:
the weights are used as they are).

Command line (JSON events, see ``jobs``; exit codes 0 done, 1 error, 2 usage, 3 cancelled)::

    python -m LesionNetworkMappingLib.coords seeds --foci FILE --bundle B --out DIR [--radius MM | --diameter MM]
        [--space MNI|TAL] [--tal-matrix pooled|spm|fsl]
    python -m LesionNetworkMappingLib.coords atrophy --wmap ID=FILE [--wmap ...] --bundle B --out DIR
        [--rule w<-2|w<-1.5|w<-2.5|top5%] [--weighted]
    python -m LesionNetworkMappingLib.coords null --foci FILE --bundle B --gm-mask PROBSEG --out DIR [--gm-space S]
        [--space S] [--k 1000] [--seed 0] [--radius MM | --diameter MM] [--rule JSON] [--sign both|pos|neg]
        [--subjects N [--random] [--subject-seed S]] [--keep-maps]
"""

import os
import sys

if __name__ == "__main__":
    from LesionNetworkMappingLib.jobs import set_blas_threads

    set_blas_threads(sys.argv[1:])  # before numpy is imported: BLAS reads these variables once

import csv
import hashlib
import json
import math
import re
import time
from typing import NamedTuple

import numpy as np

from . import jobs
from .geometry import Grid

FORMAT_SEEDS = "slicer-lnm-seeds"
FORMAT_NULL = "slicer-lnm-coordinate-null"
FORMAT_VERSION = 1
SEEDS_JSON = "seeds.json"
LESIONS_TSV = "lesions.tsv"
OUTCOMES_TSV = "outcomes.tsv"
NULL_JSON = "coords_null.json"
NULL_PEAKS_TSV = "coords_null_peaks.tsv"
SEED_FOLDER = "seeds"
KINDS = ("coordinates", "atrophy", "stimulation")
SPACES = ("MNI", "TAL")
DEFAULT_RADIUS_MM = 4.0
MAX_RADIUS_MM = 30.0
DEFAULT_SUPERSAMPLE = 5
DEFAULT_K = 1000
GM_THRESHOLD = 0.5
NA = "n/a"

#: Lancaster et al. (2007) icbm2tal matrices (MNI -> Talairach); their inverses map Talairach -> MNI.
#: "pooled" is the paper's "other" (pooled SPM+FSL) transform, the one NiMARE's tal2mni uses.
LANCASTER_MATRICES = {
    "pooled": ((0.9357, 0.0029, -0.0072, -1.0423), (-0.0065, 0.9396, -0.0726, -1.3940), (0.0103, 0.0752, 0.8967, 3.6475), (0.0, 0.0, 0.0, 1.0)),
    "spm": ((0.9254, 0.0024, -0.0118, -1.0207), (-0.0048, 0.9316, -0.0871, -1.7667), (0.0152, 0.0883, 0.8924, 4.0926), (0.0, 0.0, 0.0, 1.0)),
    "fsl": ((0.9464, 0.0034, -0.0026, -1.0680), (-0.0083, 0.9479, -0.0580, -1.0239), (0.0053, 0.0617, 0.9010, 3.1883), (0.0, 0.0, 0.0, 1.0)),
}
DEFAULT_TAL_MATRIX = "pooled"
LANCASTER_CITATION = ("Lancaster JL, Tordesillas-Gutierrez D, Martinez M, et al. Bias between MNI and Talairach coordinates analyzed using the "
                      "ICBM-152 brain template. Hum Brain Mapp 2007;28:1194-1205, doi:10.1002/hbm.20345 (inverse icbm2tal, as in NiMARE "
                      "utils.tal2mni, MIT licence)")

ATROPHY_RULES = {"w<-2": -2.0, "w<-1.5": -1.5, "w<-2.5": -2.5, "top5%": None}
DEFAULT_ATROPHY_RULE = "w<-2"
STIMULATION_KINDS = ("points", "efield", "vta")
STIMULATION_WEIGHTINGS = {"points": ("binary", "graded"), "efield": ("normalized", "threshold"), "vta": ("binary", "probabilistic")}
NULL_CAPTION = ("Random-coordinate control: null studies keep each study's number of foci, placed at random in gray matter "
                "(Ji et al. 2025; Wall et al. 2025, doi:10.1002/cns3.20108). A coordinate network that a random-coordinate "
                "network matches is not specific to the reported coordinates.")
RESEARCH_ONLY = "research-only"


class FamilyError(ValueError):
    """The gray-matter mask is not in the template family of the grid (or a space is unknown)."""


class OutsideMaskError(ValueError):
    """Seeds with no weight inside the connectome's brain mask (``labels``: their labels, in order).

    Such a seed has no connectome time course: its map would be all zero and fail the run's checks.
    """

    def __init__(self, labels):
        self.labels = list(labels)
        one = len(self.labels) == 1
        super().__init__(f"{', '.join(self.labels)} {'lies' if one else 'lie'} outside the connectome's brain mask: no connectome voxel to map from. "
                         f"Remove {'it' if one else 'them'} (or check {'its' if one else 'their'} position and space) and map again.")


class Focus(NamedTuple):
    """One reported coordinate (mm) with its study, coordinate space and optional sphere radius."""

    x: float
    y: float
    z: float
    study: str = "study"
    space: str = "MNI"
    radius_mm: float | None = None
    subjects: int | None = None

    @property
    def xyz(self):
        return (float(self.x), float(self.y), float(self.z))


#
# Small helpers
#


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _number(text):
    if text is None:
        return None
    text = str(text).strip()
    if text in ("", NA, "NA", "nan", "NaN"):
        return None
    return float(text)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_space(text, default="MNI"):
    """"MNI" or "TAL" from a space label (MNI, MNI152, ICBM, Talairach, TAL, TT, ...); ``default`` when empty."""
    if text is None or str(text).strip() in ("", NA):
        return default
    label = re.sub(r"[^a-z0-9]", "", str(text).strip().lower())
    if label.startswith(("mni", "icbm", "spm", "fsl")):
        return "MNI"
    if label.startswith(("tal", "tt")) or label == "brainmaptalairach":
        return "TAL"
    raise ValueError(f"Unknown coordinate space {text!r}: use MNI or Talairach")


def sphere_radius(radius_mm=None, diameter_mm=None):
    """(radius, record) from exactly one of a radius or a diameter in mm (4 mm radius when neither is given).

    The record states both, and which one was given: {"radius_mm", "diameter_mm", "given"}.
    """
    if radius_mm is not None and diameter_mm is not None:
        raise ValueError("Give a sphere radius or a diameter, not both")
    if diameter_mm is not None:
        radius, given = float(diameter_mm) / 2.0, "diameter"
    elif radius_mm is not None:
        radius, given = float(radius_mm), "radius"
    else:
        radius, given = DEFAULT_RADIUS_MM, "default radius"
    if not math.isfinite(radius) or radius <= 0 or radius > MAX_RADIUS_MM:
        raise ValueError(f"The sphere radius must be in (0, {MAX_RADIUS_MM:g}] mm, got {radius:g} mm")
    return radius, {"radius_mm": radius, "diameter_mm": 2.0 * radius, "given": given}


def sphere_text(record):
    """"4 mm radius (8 mm diameter)" or "3 mm radius (6 mm diameter, given as the diameter)"."""
    text = f"{record['radius_mm']:g} mm radius ({record['diameter_mm']:g} mm diameter"
    return text + (", given as the diameter)" if record.get("given") == "diameter" else ")")


#
# Parsers
#


_COLUMN_ALIASES = {
    "x": ("x", "mni_x", "tal_x", "coord_x"),
    "y": ("y", "mni_y", "tal_y", "coord_y"),
    "z": ("z", "mni_z", "tal_z", "coord_z"),
    "radius": ("radius", "radius_mm", "r", "r_mm"),
    "diameter": ("diameter", "diameter_mm", "d_mm"),
    "study": ("study", "study_id", "experiment", "contrast", "id", "group"),
    "space": ("space", "reference", "coordinate_space", "coord_space"),
    "subjects": ("subjects", "n", "sample_size"),
}
_POSITIONAL = ("x", "y", "z", "radius", "study", "space")


def _sniff_delimiter(text):
    first = next((line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")), "")
    for delimiter in ("\t", ",", ";"):
        if delimiter in first:
            return delimiter
    return None  # whitespace


def read_table(path):
    """(columns, rows) of a CSV/TSV/whitespace table; columns are lower-case; rows are dicts of text.

    A table without a header (its first field is a number) gets the positional columns x, y, z, radius, study, space.
    Lines starting with "#" are comments. A row with more (non-empty) fields than the header is an error rather than
    being cut to the header: in a comma-separated table it usually means decimal commas ("-33,5"), which would otherwise
    be read silently as other numbers.
    """
    with open(path, encoding="utf-8-sig") as f:
        text = f.read()
    delimiter = _sniff_delimiter(text)
    lines = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if delimiter is None:
        records = [line.split() for line in lines]
    else:
        records = list(csv.reader(lines, delimiter=delimiter))
    records = [[cell.strip() for cell in record] for record in records if any(cell.strip() for cell in record)]
    if not records:
        return [], []
    try:
        float(records[0][0])
        header = list(_POSITIONAL[: len(records[0])]) + [f"column{i}" for i in range(len(_POSITIONAL), len(records[0]))]
        body = records
    except ValueError:
        header = [cell.lower() for cell in records[0]]
        body = records[1:]
    first_row = len(records) - len(body) + 1  # row numbers count the header as row 1, like foci_from_rows
    rows = []
    for number, record in enumerate(body, start=first_row):
        while len(record) > len(header) and not record[-1]:
            record = record[:-1]  # trailing empty fields ("1,2,3,") are harmless
        if len(record) > len(header):
            hint = " Decimal commas? Write numbers with '.' (for example -33.5)." if delimiter == "," else ""
            raise ValueError(f"{os.path.basename(os.fspath(path))}, row {number} has {len(record)} values for {len(header)} columns "
                             f"({', '.join(header)}).{hint}")
        rows.append(dict(zip(header, record + [""] * (len(header) - len(record)), strict=True)))
    return header, rows


def _column(columns, key):
    return next((name for name in _COLUMN_ALIASES[key] if name in columns), None)


def foci_from_rows(columns, rows, default_space="MNI", default_study="study", where="the table"):
    """Foci from table rows (see :func:`read_csv`)."""
    names = {key: _column(columns, key) for key in _COLUMN_ALIASES}
    missing = [axis for axis in ("x", "y", "z") if names[axis] is None]
    if missing:
        raise ValueError(f"{where} has no {', '.join(missing)} column (columns: x, y, z[, radius, study, space])")
    if names["radius"] and names["diameter"]:
        raise ValueError(f"{where} has both a radius and a diameter column: keep one, so that the sphere size is explicit")
    foci = []
    for number, row in enumerate(rows, start=2):
        try:
            x, y, z = (_number(row.get(names[axis])) for axis in ("x", "y", "z"))
            if x is None or y is None or z is None:
                raise ValueError("a coordinate is empty")
            radius = _number(row.get(names["radius"])) if names["radius"] else None
            if names["diameter"]:
                diameter = _number(row.get(names["diameter"]))
                radius = None if diameter is None else diameter / 2.0
            space = normalize_space(row.get(names["space"]) if names["space"] else None, default_space)
            study = (row.get(names["study"]) or "").strip() if names["study"] else ""
            subjects = _number(row.get(names["subjects"])) if names["subjects"] else None
        except ValueError as exc:
            raise ValueError(f"{where}, row {number}: {exc}") from None
        foci.append(Focus(x, y, z, study or default_study, space, radius, None if subjects is None else int(subjects)))
    return foci


def read_csv(path, default_space="MNI", default_study=None):
    """Foci of a CSV/TSV table: x, y, z and optional radius (or diameter), study and space columns.

    Rows without a study belong to ``default_study`` (default: the file name without extension).
    """
    columns, rows = read_table(path)
    study = default_study or re.sub(r"\.(csv|tsv|txt)$", "", os.path.basename(os.fspath(path)), flags=re.IGNORECASE)
    return foci_from_rows(columns, rows, default_space, study, where=os.path.basename(os.fspath(path)))


_SLEUTH_REFERENCE = re.compile(r"^//\s*reference\s*=\s*(?P<space>.+?)\s*$", re.IGNORECASE)
_SLEUTH_SUBJECTS = re.compile(r"^//\s*subjects\s*=\s*(?P<n>\d+)\s*$", re.IGNORECASE)
_SLEUTH_NUMBERS = re.compile(r"[\s,;]+")


def read_sleuth(path):
    """Foci of a BrainMap Sleuth / GingerALE text file.

    ``// Reference=MNI`` (or Talairach) sets the space of the blocks after it. Each experiment block starts with
    ``//`` lines (its first one names the study, ``// Subjects=N`` gives the sample size), followed by one
    coordinate per line (a ``//`` line after coordinates starts the next block). Two blocks with the same name get " #2", " #3".
    """
    with open(path, encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    space = None
    foci, used = [], {}
    name, subjects, in_coordinates, pending = None, None, False, False

    def open_block(label):
        count = used.get(label, 0) + 1
        used[label] = count
        return label if count == 1 else f"{label} #{count}"

    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line:
            continue
        match = _SLEUTH_REFERENCE.match(line)
        if match:
            space = normalize_space(match.group("space"))
            continue
        match = _SLEUTH_SUBJECTS.match(line)
        if match:
            subjects = int(match.group("n"))
            # Before the next block's name line (some files put it first): it belongs to that block
            pending = in_coordinates or name is None
            continue
        if line.startswith("//"):
            if in_coordinates or name is None:
                name = open_block(line.lstrip("/").strip() or f"experiment{len(used) + 1}")
                subjects = subjects if pending else None
                pending = False
                in_coordinates = False
            continue
        values = [value for value in _SLEUTH_NUMBERS.split(line) if value]
        try:
            x, y, z = (float(value) for value in values[:3])
        except ValueError:
            raise ValueError(f"{os.path.basename(os.fspath(path))}, line {number}: not a coordinate: {raw!r}") from None
        if space is None:
            raise ValueError(f"{os.path.basename(os.fspath(path))}: no '// Reference=MNI' or '// Reference=Talairach' line before the coordinates")
        if name is None:
            name = open_block(f"experiment{len(used) + 1}")
        in_coordinates = True
        foci.append(Focus(x, y, z, name, space, None, subjects))
    if not foci:
        raise ValueError(f"{os.path.basename(os.fspath(path))} holds no coordinates")
    return foci


def read_nimare_json(path):
    """Foci of a NiMARE dataset JSON: {study: {"contrasts": {contrast: {"coords": {"space", "x", "y", "z"}, "metadata"}}}}.

    Each contrast is a study named "<study>-<contrast>" (NiMARE's id); "talairach"/"TAL" spaces are marked TAL.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{os.path.basename(os.fspath(path))} is not a NiMARE dataset (a JSON object of studies)")
    foci = []
    for study_id, study in data.items():
        for contrast_id, contrast in ((study or {}).get("contrasts") or {}).items():
            coords = (contrast or {}).get("coords") or {}
            xs, ys, zs = (list(coords.get(axis) or []) for axis in ("x", "y", "z"))
            if not (len(xs) == len(ys) == len(zs)):
                raise ValueError(f"{study_id}-{contrast_id}: x, y and z have different lengths")
            if not xs:
                continue
            space = normalize_space(coords.get("space"))
            sizes = ((contrast.get("metadata") or {}).get("sample_sizes") or [None]) if isinstance(contrast, dict) else [None]
            n = sizes[0] if sizes and isinstance(sizes[0], (int, float)) else None
            for x, y, z in zip(xs, ys, zs, strict=True):
                foci.append(Focus(float(x), float(y), float(z), f"{study_id}-{contrast_id}", space, None, None if n is None else int(n)))
    if not foci:
        raise ValueError(f"{os.path.basename(os.fspath(path))} holds no coordinates")
    return foci


def read_foci(path, default_space="MNI"):
    """Foci of a file: a NiMARE JSON (.json), a Sleuth text (``//`` lines) or a CSV/TSV table."""
    path = os.fspath(path)
    if path.lower().endswith(".json"):
        return read_nimare_json(path)
    with open(path, encoding="utf-8-sig") as f:
        head = f.read(4096)
    if any(line.strip().startswith("//") for line in head.splitlines()):
        return read_sleuth(path)
    return read_csv(path, default_space)


def foci_from_points(points, study="markups", space="MNI", radius_mm=None):
    """Foci from (x, y, z) points (for example a markups node's control points in RAS = MNI mm)."""
    return [Focus(float(p[0]), float(p[1]), float(p[2]), study, normalize_space(space), radius_mm) for p in points]


def group_by_study(foci):
    """{study: [foci]} in the order the studies first appear."""
    groups = {}
    for focus in foci:
        groups.setdefault(focus.study, []).append(focus)
    return groups


#
# Talairach <-> MNI
#


def _matrix(name):
    if name not in LANCASTER_MATRICES:
        raise ValueError(f"Unknown Talairach transform {name!r}: use one of {', '.join(LANCASTER_MATRICES)}")
    return np.asarray(LANCASTER_MATRICES[name], dtype=np.float64)


def _apply(matrix, xyz):
    points = np.asarray(xyz, dtype=np.float64)
    flat = points.reshape(-1, 3)
    out = flat @ matrix[:3, :3].T + matrix[:3, 3]
    return out.reshape(points.shape)


def mni2tal(xyz, matrix=DEFAULT_TAL_MATRIX):
    """MNI -> Talairach with the Lancaster 2007 icbm2tal transform (``matrix``: pooled, spm or fsl)."""
    return _apply(_matrix(matrix), xyz)


def tal2mni(xyz, matrix=DEFAULT_TAL_MATRIX):
    """Talairach -> MNI: the inverse of the Lancaster 2007 icbm2tal transform (NiMARE tal2mni with "pooled")."""
    return _apply(np.linalg.inv(_matrix(matrix)), xyz)


def to_mni(foci, matrix=DEFAULT_TAL_MATRIX):
    """(foci in MNI, conversion record). Talairach foci are converted; the record says how many and how."""
    converted, out = 0, []
    for focus in foci:
        if focus.space == "TAL":
            x, y, z = tal2mni(focus.xyz, matrix)
            out.append(focus._replace(x=float(x), y=float(y), z=float(z), space="MNI"))
            converted += 1
        else:
            out.append(focus)
    record = {"converted": converted, "total": len(foci), "from": "Talairach" if converted else None, "to": "MNI"}
    if converted:
        inverse = np.linalg.inv(_matrix(matrix))
        record.update(method=f"inverse Lancaster 2007 icbm2tal ({matrix})", matrix=matrix, tal2mni=np.round(inverse, 8).tolist(), citation=LANCASTER_CITATION,
                      studies=sorted({focus.study for focus in foci if focus.space == "TAL"}))
    return out, record


def conversion_text(record):
    """One line for the Seed card and methods: "12 of 40 foci converted from Talairach to MNI (inverse Lancaster ...)"."""
    if not record or not record.get("converted"):
        return "All foci in MNI space (no conversion)."
    return f"{record['converted']} of {record['total']} foci converted from Talairach to MNI ({record['method']}; Lancaster et al. 2007)."


#
# Spheres (fractional occupancy through the grid's affine)
#


def _as_grid(grid):
    if isinstance(grid, Grid):
        return grid
    if hasattr(grid, "shape") and hasattr(grid, "affine"):
        return Grid(grid.shape, grid.affine)
    shape, affine = grid
    return Grid(shape, affine)


def _subsample_offsets(supersample):
    s = max(1, int(supersample))
    steps = (np.arange(s) + 0.5) / s - 0.5
    return np.stack(np.meshgrid(steps, steps, steps, indexing="ij"), axis=-1).reshape(-1, 3)


def sphere_occupancy(center_xyz, radius_mm, grid, supersample=DEFAULT_SUPERSAMPLE, graded=False, out=None):
    """Occupancy of each voxel of ``grid`` by a sphere (fraction of the voxel's sub-samples inside it), or with
    ``graded`` the mean of max(0, 1 - d / radius) over the sub-samples (a linearly graded sphere). Voxels of
    ``out`` (if given) keep the larger value (a union). Returns the float32 volume indexed [i, j, k].
    """
    grid = _as_grid(grid)
    radius = float(radius_mm)
    if out is None:
        out = np.zeros(grid.shape, dtype=np.float32)
    linear, offset = grid.affine[:3, :3], grid.affine[:3, 3]
    center = np.asarray(center_xyz, dtype=np.float64)
    voxel = np.linalg.solve(linear, center - offset)
    reach = np.ceil(radius / np.maximum(grid.spacing, 1e-9)).astype(int) + 1
    lo = np.maximum(np.floor(voxel).astype(int) - reach, 0)
    hi = np.minimum(np.ceil(voxel).astype(int) + reach, np.array(grid.shape) - 1)
    if np.any(lo > hi):
        return out
    ijk = np.stack(np.meshgrid(*[np.arange(a, b + 1) for a, b in zip(lo, hi, strict=True)], indexing="ij"), axis=-1).reshape(-1, 3)
    subs = _subsample_offsets(supersample)
    values = np.empty(len(ijk), dtype=np.float64)
    step = max(1, 2_000_000 // len(subs))
    for start in range(0, len(ijk), step):
        points = (ijk[start:start + step, None, :] + subs[None, :, :]) @ linear.T + offset
        distance = np.linalg.norm(points - center, axis=2)
        if graded:
            values[start:start + step] = np.clip(1.0 - distance / radius, 0.0, 1.0).mean(axis=1)
        else:
            values[start:start + step] = (distance <= radius).mean(axis=1)
    keep = values > 0
    i, j, k = ijk[keep].T
    out[i, j, k] = np.maximum(out[i, j, k], values[keep].astype(np.float32))
    return out


def spheres(foci, grid, radius_mm=None, diameter_mm=None, supersample=DEFAULT_SUPERSAMPLE, graded=False, use_focus_radius=True):
    """Union of spheres around foci (MNI mm = the grid's world mm) as fractional occupancy on ``grid``.

    Exactly one of ``radius_mm`` and ``diameter_mm`` (4 mm radius when neither is given); a focus with its own
    radius (a radius or diameter column) uses it when ``use_focus_radius``. Talairach foci are refused: convert
    them first (:func:`to_mni`). Returns (volume float32 [i, j, k], sphere record of :func:`sphere_radius`).
    """
    grid = _as_grid(grid)
    radius, record = sphere_radius(radius_mm, diameter_mm)
    out = np.zeros(grid.shape, dtype=np.float32)
    own = set()
    for focus in foci:
        if getattr(focus, "space", "MNI") == "TAL":
            raise ValueError("Talairach foci must be converted to MNI first (coords.to_mni)")
        r = radius
        if use_focus_radius and getattr(focus, "radius_mm", None):
            r = sphere_radius(focus.radius_mm)[0]
            own.add(r)
        xyz = focus.xyz if hasattr(focus, "xyz") else tuple(float(v) for v in focus[:3])
        sphere_occupancy(xyz, r, grid, supersample, graded, out)
    if own:
        record = dict(record, per_focus_radii_mm=sorted(own))
    return out, record


def masked(volume, mask_indices):
    """In-mask vector of a volume (``mask_indices``: flat C-order indices, e.g. ``bundle.mask_indices``)."""
    return np.asarray(volume, dtype=np.float32).reshape(-1)[np.asarray(mask_indices)]


def study_union_seeds(foci_by_study, grid, radius_mm=None, diameter_mm=None, mask_indices=None, supersample=DEFAULT_SUPERSAMPLE):
    """Study-level union seeds: (names, weights (S, V) float32, sphere record).

    Row s is the union of the spheres of study s (its fractional occupancy) over the grid's voxels, or over
    ``mask_indices`` (the connectome voxels, so that the rows are the engine's seed columns: one pass).
    """
    grid = _as_grid(grid)
    names = list(foci_by_study)
    n_columns = grid.n_voxels if mask_indices is None else len(mask_indices)
    weights = np.zeros((len(names), n_columns), dtype=np.float32)
    record = sphere_radius(radius_mm, diameter_mm)[1]
    own = set()
    for row, name in enumerate(names):
        volume, study_record = spheres(foci_by_study[name], grid, radius_mm, diameter_mm, supersample)
        own.update(study_record.get("per_focus_radii_mm", ()))
        weights[row] = volume.reshape(-1) if mask_indices is None else masked(volume, mask_indices)
    if own:
        record = dict(record, per_focus_radii_mm=sorted(own))
    return names, weights, record


def focus_radii(foci, radius_mm=None, diameter_mm=None):
    """(F,) sphere radius of each focus, as :func:`spheres` uses it: its own radius when it has one, else the given one."""
    radius = sphere_radius(radius_mm, diameter_mm)[0]
    return np.array([sphere_radius(focus.radius_mm)[0] if getattr(focus, "radius_mm", None) else radius for focus in foci], dtype=np.float64)


#
# Random-coordinate null
#


def gm_mask_for(gm, grid, gm_space=None, target_space=None, threshold=GM_THRESHOLD):
    """Boolean gray-matter mask on ``grid``: GM prior probability > ``threshold`` (resampled when on another grid).

    ``gm`` is a NIfTI path (its space read from a TemplateFlow name when ``gm_space`` is None), an (array, affine)
    pair or an array on ``grid``. The mask must be of the template family of ``target_space`` (the grid's space):
    otherwise :class:`FamilyError` (templates of different families differ by a few millimetres).
    """
    from .circuits import FamilyError as CircuitsFamilyError
    from .circuits import check_families, gm_mask_on_grid, space_of_file

    grid = _as_grid(grid)
    if gm_space is None and isinstance(gm, (str, os.PathLike)):
        gm_space = space_of_file(gm)
    try:
        check_families(target_space, gm_space)
    except CircuitsFamilyError as exc:
        raise FamilyError(str(exc).replace("circuit", "connectome grid")) from None
    mask, _resampled = gm_mask_on_grid(gm, grid.shape, grid.affine, threshold)
    if not mask.any():
        raise ValueError(f"The gray-matter mask has no voxel with probability > {threshold:g} on this grid")
    return mask


def random_coordinate_sets(counts, gm_mask, grid, k=DEFAULT_K, seed=0):
    """``k`` null sets of foci: (k, F, 3) world coordinates (mm) and the (F,) study index of each focus.

    ``counts`` is {study: number of foci} (or {study: [foci]}): every null set has the same number of foci per
    study, placed at gray-matter voxel centres drawn uniformly (distinct within a study when there are enough voxels).
    """
    grid = _as_grid(grid)
    names = list(counts)
    sizes = [len(counts[name]) if not isinstance(counts[name], (int, np.integer)) else int(counts[name]) for name in names]
    voxels = np.argwhere(np.asarray(gm_mask, dtype=bool).reshape(grid.shape))
    if not len(voxels):
        raise ValueError("The gray-matter mask is empty")
    rng = np.random.default_rng(int(seed))
    study_index = np.repeat(np.arange(len(names)), sizes)
    out = np.empty((int(k), int(sum(sizes)), 3), dtype=np.float64)
    for draw in range(int(k)):
        start = 0
        for size in sizes:
            chosen = rng.choice(len(voxels), size=size, replace=size > len(voxels))
            out[draw, start:start + size] = voxels[chosen] @ grid.affine[:3, :3].T + grid.affine[:3, 3]
            start += size
    return out, study_index, names


def seed_overlap_fn(weights):
    """The default ``network_fn`` of :func:`random_coordinate_null`: the seeds themselves (weight > 0)."""
    return np.asarray(weights) > 0


def engine_network_fn(bundle, rule=None, sign="both", subjects=None, random_subset=False, seed=0, cancel=None):
    """``network_fn`` that maps seed rows with the engine (one pass per call) and binarizes each map with the threshold
    ``rule`` (thresholds.py; on T, or Fz for the rules on Fz) and ``sign``. Returns (fn, info {n, rule}).
    """
    from . import cohort, engine, thresholds

    rule = thresholds.normalize_rule(rule)
    ids = engine.resolve_subjects(bundle, subjects, random_subset, seed)
    statistic = cohort.rule_statistic(rule)

    def network_fn(weights):
        maps = engine.compute_network_maps(bundle, np.asarray(weights, dtype=np.float32), subjects=ids, cancel=cancel)
        if maps.cancelled:
            raise jobs.Cancelled(reason="cancelled")
        values = np.asarray(maps.t if statistic == "T" else maps.avg_fz, dtype=np.float32)
        resolved = cohort.resolve_rules(values, rule, len(ids), bundle.n_voxels)
        return cohort.binarize_maps(values, [r.value for r in resolved], sign)

    return network_fn, {"n": len(ids), "rule": rule, "statistic": statistic, "sign": sign}


def _percentile(observed, null):
    null = np.asarray(null, dtype=np.float64)
    null = null[np.isfinite(null)]
    if not null.size or not math.isfinite(observed):
        return float("nan")
    return 100.0 * (float((null < observed).sum()) + 0.5 * float((null == observed).sum())) / null.size


def random_coordinate_null(foci_by_study, gm_mask, k=DEFAULT_K, seed=0, grid=None, network_fn=None, radius_mm=None, diameter_mm=None,
                           mask_indices=None, columns_per_call=100, keep_maps=False, cancel=None, progress=None, supersample=DEFAULT_SUPERSAMPLE):
    """Random-coordinate control of a coordinate network map.

    The observed studies' union seeds and ``k`` null sets (:func:`random_coordinate_sets`: the same number of foci
    per study, in gray matter) go through ``network_fn(weights (S, V)) -> (S, V) bool`` (default: the seeds
    themselves; :func:`engine_network_fn` maps them with a connectome), at most ``columns_per_call`` seed rows per
    call. The overlap is the percent of studies connected at each voxel.

    :param gm_mask: boolean GM mask on ``grid`` (see :func:`gm_mask_for`, which enforces the template family).
    :return: dict with ``observed`` (V,) percent, ``observed_peak``, ``null_peaks`` (k,), ``percentile`` (of the
      observed peak among the null peaks, 0-100), ``p_peak`` = (1 + #{null peak >= observed}) / (k + 1),
      ``voxel_p`` (V,) = (1 + #{null overlap >= observed}) / (k + 1), ``null_mean`` (V,), ``null_maps`` ((k, V)
      float32 percent, with ``keep_maps``), ``counts`` {study: foci}, ``k``, ``seed``, ``sphere``.
    """
    if grid is None:
        raise ValueError("random_coordinate_null needs the grid of the seeds")
    grid = _as_grid(grid)
    network_fn = network_fn or seed_overlap_fn
    names = list(foci_by_study)
    if not names:
        raise ValueError("No study to compare")
    _names, observed_w, sphere = study_union_seeds(foci_by_study, grid, radius_mm, diameter_mm, mask_indices, supersample)
    # Null foci keep the sphere of the observed focus they replace (same study, same position in the study), so a
    # radius column in the file applies to the null spheres as it does to the observed ones: like with like.
    radii = np.concatenate([focus_radii(foci_by_study[name], radius_mm, diameter_mm) for name in names])
    observed = 100.0 * np.asarray(network_fn(observed_w), dtype=bool).mean(axis=0)
    observed_peak = float(observed.max()) if observed.size else 0.0
    sets, study_index, _ = random_coordinate_sets(foci_by_study, gm_mask, grid, k, seed)
    n_studies = len(names)
    per_call = max(1, int(columns_per_call) // n_studies)
    null_peaks = np.empty(int(k), dtype=np.float64)
    exceed = np.zeros(observed.shape, dtype=np.int64)
    total = np.zeros(observed.shape, dtype=np.float64)
    null_maps = np.empty((int(k), observed.size), dtype=np.float32) if keep_maps else None
    started = time.monotonic()
    for first in range(0, int(k), per_call):
        if cancel is not None and (cancel() if callable(cancel) else cancel.is_set()):
            raise jobs.Cancelled(done=first)
        draws = range(first, min(int(k), first + per_call))
        rows = []
        for draw in draws:
            for study in range(n_studies):
                volume = np.zeros(grid.shape, dtype=np.float32)
                in_study = study_index == study
                for xyz, r in zip(sets[draw][in_study], radii[in_study], strict=True):
                    sphere_occupancy(xyz, r, grid, supersample, False, volume)
                rows.append(volume.reshape(-1) if mask_indices is None else masked(volume, mask_indices))
        binary = np.asarray(network_fn(np.stack(rows)), dtype=bool).reshape(len(draws), n_studies, -1)
        for offset, draw in enumerate(draws):
            overlap = 100.0 * binary[offset].mean(axis=0)
            null_peaks[draw] = float(overlap.max()) if overlap.size else 0.0
            exceed += overlap >= observed - 1e-9
            total += overlap
            if null_maps is not None:
                null_maps[draw] = overlap
        if progress is not None:
            progress(draws[-1] + 1, int(k), time.monotonic() - started)
    return {
        "observed": observed.astype(np.float32),
        "observed_peak": observed_peak,
        "null_peaks": null_peaks,
        "percentile": _percentile(observed_peak, null_peaks),
        "p_peak": (1.0 + float((null_peaks >= observed_peak - 1e-9).sum())) / (int(k) + 1.0),
        "voxel_p": ((1.0 + exceed) / (int(k) + 1.0)).astype(np.float32),
        "null_mean": (total / max(int(k), 1)).astype(np.float32),
        "null_maps": null_maps,
        "counts": {name: len(foci_by_study[name]) for name in names},
        "k": int(k),
        "seed": int(seed),
        "sphere": sphere,
    }


#
# Atrophy
#


def atrophy_rule(rule):
    """Normalized atrophy rule: "w<-2", "w<-1.5", "w<-2.5" or "top5%" (also from a number such as -2)."""
    if isinstance(rule, (int, float)):
        rule = f"w<{float(rule):g}"
    text = re.sub(r"\s+", "", str(rule or DEFAULT_ATROPHY_RULE)).lower().replace("−", "-")  # noqa: RUF001 (a minus sign)
    if text in ("top5", "top5%", "top5pct"):
        return "top5%"
    if text not in ATROPHY_RULES:
        raise ValueError(f"Unknown atrophy rule {rule!r}: use one of {', '.join(ATROPHY_RULES)}")
    return text


def atrophy_seed(wmap, rule=DEFAULT_ATROPHY_RULE, weighted=False, mask=None):
    """Seed of a per-patient w-map (or z-map; negative = atrophy).

    ``rule``: ``w<-2`` (Tetreault 2020), ``w<-1.5``, ``w<-2.5`` or ``top5%`` (the 5 % most negative in-mask values,
    among the negative ones). ``mask``: the voxels considered (default: finite, non-zero values). Binary: 1 in the
    seed; ``weighted``: -w scaled so that the most atrophied voxel weighs 1. Returns (float32 seed, record).
    """
    rule = atrophy_rule(rule)
    w = np.asarray(wmap, dtype=np.float64)
    support = np.isfinite(w) & (w != 0) if mask is None else (np.asarray(mask, dtype=bool) & np.isfinite(w))
    values = np.where(support, w, 0.0)
    if rule == "top5%":
        negative = values[support & (values < 0)]
        cut = float(np.percentile(values[support], 5.0)) if support.any() else 0.0
        selected = support & (values < 0) & (values <= cut) if negative.size else np.zeros_like(support)
    else:
        cut = ATROPHY_RULES[rule]
        selected = support & (values < cut)
    seed = np.zeros(w.shape, dtype=np.float32)
    if selected.any():
        if weighted:
            strength = -values[selected]
            seed[selected] = (strength / max(float(strength.max()), 1e-12)).astype(np.float32)
        else:
            seed[selected] = 1.0
    record = {"rule": rule, "cut": cut, "weighted": bool(weighted), "voxels": int(selected.sum()), "support_voxels": int(support.sum())}
    return seed, record


#
# Stimulation
#


#: NIfTI xform code of MNI-152 coordinates (NIFTI_XFORM_MNI_152)
_XFORM_MNI_152 = 4


def stimulation_image_space(image, header=None):
    """Where a stimulation image (E-field or VTA) is, as far as its file can tell: a dict with ``mni`` (True when the
    image is on a known template grid (geometry.detect_space) or its NIfTI header marks it MNI-152 (sform or qform code
    4), else False: subject/scanner space cannot be ruled out), ``space`` (the template label, "MNI152 (NIfTI header)"
    or None), ``basis`` ("grid", "header" or None) and ``oblique`` (a rotated voxel grid: the usual sign of a
    scanner-space image; template images are axis-aligned).

    ``image``: a NIfTI path (only its header is read, unless ``header`` is given) or (array, affine). Nothing is
    refused: an MNI image on its own small grid (a Lead-DBS VTA) also has no known grid, so the caller tells the user
    (:func:`stimulation_space_notice`) and the seed record keeps the result.
    """
    from .geometry import detect_space

    if isinstance(image, (str, os.PathLike)):
        if header is None:
            from .nifti import read_nifti_header

            header = read_nifti_header(image)
        shape, affine = tuple(header["shape"][:3]), np.asarray(header["affine"], dtype=np.float64)
    else:
        data, affine = image
        shape, affine = tuple(np.shape(data)[:3]), np.asarray(affine, dtype=np.float64)
    columns = affine[:3, :3] / np.maximum(np.linalg.norm(affine[:3, :3], axis=0), 1e-12)
    oblique = bool(np.any(np.abs(columns).max(axis=0) < 1.0 - 1e-4))  # an axis not along x, y or z (more than ~0.8 degrees)
    template = detect_space((shape, affine))
    if template is not None:
        return {"mni": True, "space": str(template[0]), "basis": "grid", "oblique": oblique}
    if header is not None:
        source = header.get("affine_source")
        code = header.get("sform_code") if source == "sform" else header.get("qform_code") if source == "qform" else 0
        if code == _XFORM_MNI_152:
            return {"mni": True, "space": "MNI152 (NIfTI header)", "basis": "header", "oblique": oblique}
    return {"mni": False, "space": None, "basis": None, "oblique": oblique}


def stimulation_space_notice(names_and_spaces):
    """Amber notice for stimulation images not known to be in MNI space, or "" when all are: ``names_and_spaces`` is
    [(name, :func:`stimulation_image_space` dict)].
    """
    unknown = [name for name, space in names_and_spaces if space is not None and not space.get("mni")]
    if not unknown:
        return ""
    oblique = [name for name, space in names_and_spaces if space is not None and not space.get("mni") and space.get("oblique")]
    shown = ", ".join(unknown[:4]) + (f" and {len(unknown) - 4} more" if len(unknown) > 4 else "")
    one = len(unknown) == 1
    text = (f"Not confirmed as MNI space: {shown} (not on a known MNI grid and not marked MNI in the NIfTI header). "
            + ("It is mapped as if its world mm were MNI mm" if one else "They are mapped as if their world mm were MNI mm")
            + "; an image in subject (scanner) space gives a misplaced seed. Use MNI-space files (SimNIBS: the *_MNI field; Lead-DBS: the MNI VTA).")
    if oblique:
        text += (" It is on an oblique grid, typical of scanner space." if one
                 else f" {len(oblique)} of {len(unknown)} are on an oblique grid, typical of scanner space.")
    return text


def stimulation_seed(kind, grid, points=None, image=None, radius_mm=None, diameter_mm=None, weighting=None, threshold=None, supersample=DEFAULT_SUPERSAMPLE):
    """Seed of one stimulation site on ``grid`` (float32 weights in [0, 1]) and its record.

    - ``points``: (x, y, z) points (MNI mm) with a sphere radius or diameter; ``weighting`` "binary" (occupancy) or
      "graded" (1 at the centre to 0 at the radius).
    - ``efield``: an E-field NIfTI path or (array, affine) (SimNIBS, V/m); "normalized" (E / max E) or "threshold"
      (E >= ``threshold`` V/m; default half the maximum).
    - ``vta``: a VTA NIfTI (Lead-DBS); "binary" (occupancy >= 0.5 after resampling) or "probabilistic" (values in
      [0, 1]).

    Images are resampled onto the grid by the volume-weighted mean (geometry.resample_to_grid "fraction"). Their world
    mm are used as MNI mm; the record's ``space`` (:func:`stimulation_image_space`) says whether that is confirmed.
    """
    from .geometry import resample_to_grid

    if kind not in STIMULATION_KINDS:
        raise ValueError(f"Unknown stimulation seed {kind!r}: use one of {', '.join(STIMULATION_KINDS)}")
    weighting = weighting or STIMULATION_WEIGHTINGS[kind][0]
    if weighting not in STIMULATION_WEIGHTINGS[kind]:
        raise ValueError(f"A {kind} seed is weighted by {' or '.join(STIMULATION_WEIGHTINGS[kind])}, not {weighting!r}")
    grid = _as_grid(grid)
    record = {"kind": kind, "weighting": weighting}
    if kind == "points":
        if not points:
            raise ValueError("A points seed needs at least one point")
        foci = [p if isinstance(p, Focus) else Focus(float(p[0]), float(p[1]), float(p[2])) for p in points]
        seed, sphere = spheres(foci, grid, radius_mm, diameter_mm, supersample, graded=weighting == "graded")
        record.update(sphere=sphere, points=len(foci))
        return seed, record
    if image is None:
        raise ValueError(f"A {kind} seed needs an image")
    if isinstance(image, (str, os.PathLike)):
        from .nifti import read_nifti

        data, affine, header = read_nifti(image)
        record["source"] = {"file": os.path.basename(os.fspath(image)), "sha256": _sha256(image)}
        record["space"] = stimulation_image_space(image, header)
    else:
        data, affine = image
        record["space"] = stimulation_image_space((data, affine))
    data = np.nan_to_num(np.asarray(data, dtype=np.float64))
    if data.ndim > 3 and data.shape[-1] == 3 and int(np.prod(data.shape[3:])) == 3:
        if kind != "efield":
            raise ValueError("A VTA image must be one 3D volume, not a vector image")
        data = np.linalg.norm(data, axis=-1)  # a vector E-field (SimNIBS *_E.nii.gz): its magnitude, as magnE
    if data.ndim > 3 and int(np.prod(data.shape[3:])) != 1:
        raise ValueError(f"The {kind} image must be one 3D volume (or a 3-component vector E-field), not shape {tuple(data.shape)}")
    data = data.reshape(data.shape[:3])
    on_grid = data if grid.matches(Grid(data.shape, affine)) else resample_to_grid(data, affine, grid, mode="fraction")
    on_grid = np.asarray(on_grid, dtype=np.float64)
    if kind == "efield":
        on_grid = np.abs(on_grid)
        peak = float(on_grid.max()) if on_grid.size else 0.0
        if peak <= 0:
            raise ValueError("The E-field is zero everywhere on the connectome grid")
        if weighting == "threshold":
            cut = float(threshold) if threshold is not None else 0.5 * peak
            seed = (on_grid >= cut).astype(np.float32)
            record.update(threshold_v_per_m=cut, threshold_given=threshold is not None)
        else:
            seed = np.clip(on_grid / peak, 0.0, 1.0).astype(np.float32)
        record["peak_v_per_m"] = peak
    else:
        seed = (on_grid >= 0.5).astype(np.float32) if weighting == "binary" else np.clip(on_grid, 0.0, 1.0).astype(np.float32)
    if not seed.any():
        raise ValueError(f"The {kind} seed is empty on the connectome grid")
    return seed, record


def outcome_rmap(fz_maps, outcomes, flip=False, covariates=None):
    """R-map: the (partial) correlation over patients of each voxel's Fz with the outcome (Horn 2017).

    ``flip`` multiplies the outcome by -1 (TMS: a larger anticorrelation means a better response).
    """
    from .slnm import partial_r_map

    x = np.asarray(outcomes, dtype=np.float64) * (-1.0 if flip else 1.0)
    Y = np.asarray(fz_maps, dtype=np.float64)
    if Y.shape[0] != x.size or x.size < 3:
        raise ValueError(f"An R-map needs one map per outcome and at least 3 patients (got {Y.shape[0]} maps, {x.size} outcomes)")
    Z = None if covariates is None else np.asarray(covariates, dtype=np.float64).reshape(x.size, -1)
    return partial_r_map(x, Y, Z)


def outcome_weighted_map(fz_maps, outcomes, flip=False):
    """Outcome-weighted average map: sum(o_i Fz_i) / sum(|o_i|) (Horn 2017's weighted average; descriptive)."""
    x = np.asarray(outcomes, dtype=np.float64) * (-1.0 if flip else 1.0)
    Y = np.asarray(fz_maps, dtype=np.float64)
    if Y.shape[0] != x.size or not np.any(x):
        raise ValueError("The outcome-weighted map needs one map per outcome and a non-zero outcome")
    return (x @ Y / np.abs(x).sum()).astype(np.float32)


def write_outcome_table(path, outcomes, column="outcome"):
    """Write a participants-style TSV (participant_id, ``column``) of the per-patient outcomes; return ``path``."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", column):
        raise ValueError(f"An outcome column name has letters, digits and underscores: {column!r}")
    lines = [f"participant_id\t{column}"]
    for sid, value in outcomes.items():
        number = _number(value)
        lines.append(f"{sid}\t{NA if number is None or not math.isfinite(number) else repr(float(number))}")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return path


def slnm_arguments(maps_tsv, participants, column, mask, out_dir, flip=False, perm=5000, bundle=None):
    """Arguments of ``python -m LesionNetworkMappingLib.slnm`` for an R-map of stimulation seeds (continuous outcome on
    Fz, permutation inference). Seed volume is not a covariate (it is set by the stimulation model, not by damage).
    """
    args = ["--maps", os.fspath(maps_tsv), "--participants", os.fspath(participants), "--score", column, "--covariates", "",
            "--volume-off-reason", "stimulation seeds: the seed size is set by the stimulation model, not by damage",
            "--input", "Fz", "--perm", str(int(perm)), "--mask", os.fspath(mask), "--out", os.fspath(out_dir)]
    if flip:
        args.append("--flip")
    if bundle:
        args += ["--bundle", os.fspath(bundle)]
    return args


#
# Run folder: seeds, lesions.tsv and seeds.json
#


def seed_id(label, used=None):
    """A participant id for a seed from its label (letters, digits and "-"; unique among ``used``)."""
    base = re.sub(r"[^A-Za-z0-9]+", "-", str(label)).strip("-")[:48] or "seed"
    if used is None:
        return base
    name, counter = base, 2
    while name.lower() in used:
        name = f"{base}-{counter}"
        counter += 1
    used.add(name.lower())
    return name


def write_seed_set(out_dir, seeds, grid, kind, record=None, mask_indices=None):
    """Write seed NIfTIs, ``lesions.tsv`` and ``seeds.json`` in ``out_dir``; return the path of lesions.tsv.

    ``seeds``: [(participant_id, label, volume float [i, j, k], extra dict)] on ``grid``. Paths in the files are
    relative to ``out_dir``; sources are named by file name and sha256 only. ``mask_indices`` (the connectome's
    in-mask voxels, flat C-order, e.g. ``bundle.mask_indices``): when given, seeds with no weight in the mask are
    refused before anything is written (:class:`OutsideMaskError`, naming every such seed by its label).
    """
    from .nifti import write_nifti

    if kind not in KINDS:
        raise ValueError(f"Unknown seed kind {kind!r}")
    grid = _as_grid(grid)
    if mask_indices is not None:
        indices = np.asarray(mask_indices)
        outside = [str(label) for _sid, label, volume, _extra in seeds
                   if np.any(np.asarray(volume) > 0) and not np.any(np.asarray(volume, dtype=np.float32).reshape(-1)[indices] > 0)]
        if outside:
            raise OutsideMaskError(outside)
    out_dir = os.path.abspath(os.fspath(out_dir))
    folder = os.path.join(out_dir, SEED_FOLDER)
    os.makedirs(folder, exist_ok=True)
    rows, entries, seen = ["participant_id\tlesion\tlesion_sha256"], [], set()
    voxel_ml = grid.voxel_volume / 1000.0
    for sid, label, volume, extra in seeds:
        if sid in seen:
            raise ValueError(f"Two seeds have the id {sid!r}")
        seen.add(sid)
        volume = np.asarray(volume, dtype=np.float32).reshape(grid.shape)
        if not np.any(volume > 0):
            raise ValueError(f"The seed {label!r} is empty on the connectome grid")
        path = os.path.join(folder, f"{sid}_seed.nii.gz")
        write_nifti(path, volume, grid.affine, dtype=np.float32, descrip=f"LNM {kind} seed")
        sha = _sha256(path)
        rows.append(f"{sid}\t{SEED_FOLDER}/{sid}_seed.nii.gz\t{sha}")
        entries.append(dict({"participant_id": sid, "label": str(label), "file": f"{SEED_FOLDER}/{sid}_seed.nii.gz", "sha256": sha,
                             "voxels": int((volume > 0).sum()), "weight_sum": round(float(volume.sum()), 4),
                             "volume_ml": round(float(volume.sum()) * voxel_ml, 4)}, **(extra or {})))
    tsv = os.path.join(out_dir, LESIONS_TSV)
    with open(tsv, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(rows) + "\n")
    from .export import LIBRARY_VERSION

    document = {"format": FORMAT_SEEDS, "version": FORMAT_VERSION, "kind": kind, "created": _now(), "library_version": LIBRARY_VERSION,
                "grid": {"shape": list(grid.shape), "affine": np.round(grid.affine, 6).tolist()}, "seeds": entries, "research_use": RESEARCH_ONLY}
    document.update(record or {})
    jobs.write_json_atomic(os.path.join(out_dir, SEEDS_JSON), document)
    return tsv


def read_seed_record(out_dir):
    """seeds.json of a run folder, or None."""
    try:
        with open(os.path.join(os.fspath(out_dir), SEEDS_JSON), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _source(path):
    return {"file": os.path.basename(os.fspath(path)), "sha256": _sha256(path)} if path and os.path.isfile(path) else None


def prepare_coordinates(out_dir, foci, grid, radius_mm=None, diameter_mm=None, tal_matrix=DEFAULT_TAL_MATRIX, source=None, grid_space=None, supersample=DEFAULT_SUPERSAMPLE,
                        mask_indices=None):
    """Study-level union seeds of ``foci`` in ``out_dir`` (Talairach foci converted, recorded); return lesions.tsv.

    ``mask_indices``: refuse studies whose seed has no weight in the connectome's brain mask (:func:`write_seed_set`).
    """
    if not foci:
        raise ValueError("No coordinates")
    mni, conversion = to_mni(foci, tal_matrix)
    groups = group_by_study(mni)
    seeds, used = [], set()
    sphere = sphere_radius(radius_mm, diameter_mm)[1]
    for name, members in groups.items():
        volume, sphere = spheres(members, grid, radius_mm, diameter_mm, supersample)
        seeds.append((seed_id(name, used), name, volume, {"n_foci": len(members), "space_reported": sorted({f.space for f in foci if f.study == name})}))
    record = {"sphere": sphere, "sphere_text": sphere_text(sphere), "conversion": conversion, "conversion_text": conversion_text(conversion),
              "studies": len(groups), "foci": len(foci), "seed_rule": "study-level union of spheres (Darby et al. 2019)",
              "placement": f"MNI coordinates are used as the world mm of the connectome grid ({grid_space or 'space not stated'}); no template-family warp",
              "source": _source(source)}
    return write_seed_set(out_dir, seeds, grid, "coordinates", record, mask_indices)


def prepare_atrophy(out_dir, wmaps, grid, rule=DEFAULT_ATROPHY_RULE, weighted=False, mask_indices=None):
    """Atrophy seeds of per-patient w-maps {participant_id: path} on ``grid``; return lesions.tsv (``mask_indices``: see :func:`write_seed_set`)."""
    from .geometry import resample_to_grid
    from .nifti import read_nifti

    grid = _as_grid(grid)
    seeds, used, sources = [], set(), {}
    rule = atrophy_rule(rule)
    for sid, path in wmaps.items():
        data, affine, _header = read_nifti(path)
        data = np.asarray(data, dtype=np.float64).reshape(data.shape[:3])
        seed, record = atrophy_seed(data, rule, weighted)
        if not grid.matches(Grid(seed.shape, affine)):
            seed = resample_to_grid(seed, affine, grid, mode="fraction").astype(np.float32)
            if not weighted:
                seed = (seed >= 0.5).astype(np.float32)
        seeds.append((seed_id(sid, used), sid, seed, {"atrophy": record}))
        sources[sid] = _source(path)
    return write_seed_set(out_dir, seeds, grid, "atrophy", {"rule": rule, "weighted": bool(weighted), "sources": sources,
                                                            "seed_rule": "per-patient w-map thresholded (Tetreault et al. 2020)"}, mask_indices)


def prepare_stimulation(out_dir, items, grid, kind, radius_mm=None, diameter_mm=None, weighting=None, threshold=None, outcome_column="outcome", mask_indices=None):
    """Stimulation seeds of ``items`` [{participant_id, points | image, outcome}] in ``out_dir``; return lesions.tsv.

    Points of rows with the same participant_id form one seed (a union). The outcomes, when given, are written to
    ``outcomes.tsv`` (for :func:`slnm_arguments`). ``mask_indices``: refuse participants whose seed has no weight
    in the connectome's brain mask (:func:`write_seed_set`).
    """
    grid = _as_grid(grid)
    merged = {}
    for item in items:
        sid = str(item.get("participant_id") or "").strip()
        if not sid:
            raise ValueError("Every stimulation row needs a participant_id")
        entry = merged.setdefault(sid, {"points": [], "image": None, "outcome": None})
        if item.get("points"):
            entry["points"].extend(item["points"])
        if item.get("image") is not None:
            if entry["image"] is not None:
                raise ValueError(f"{sid} has two stimulation images")
            entry["image"] = item["image"]
        if item.get("outcome") not in (None, ""):
            entry["outcome"] = item["outcome"]
    seeds, used, outcomes, records = [], set(), {}, {}
    for sid, entry in merged.items():
        seed, record = stimulation_seed(kind, grid, points=entry["points"] or None, image=entry["image"], radius_mm=radius_mm, diameter_mm=diameter_mm,
                                        weighting=weighting, threshold=threshold)
        pid = seed_id(sid, used)
        seeds.append((pid, sid, seed, {"stimulation": record}))
        records[pid] = record
        if entry["outcome"] is not None:
            outcomes[pid] = entry["outcome"]
    unconfirmed = [pid for pid, record in records.items() if record.get("space") is not None and not record["space"].get("mni")]
    if kind == "points":
        placement = "MNI coordinates are used as the world mm of the connectome grid; no template-family warp"
    else:
        placement = ("Image world mm are used as the world mm of the connectome grid (MNI); no warp. "
                     + (f"Not confirmed as MNI space: {', '.join(unconfirmed)}" if unconfirmed else "Every image is on a known MNI grid or marked MNI"))
    tsv = write_seed_set(out_dir, seeds, grid, "stimulation", {
        "stimulation": {"kind": kind, "weighting": weighting or STIMULATION_WEIGHTINGS[kind][0],
                        "sphere": sphere_radius(radius_mm, diameter_mm)[1] if kind == "points" else None,
                        "placement": placement, "space_unconfirmed": unconfirmed},
        "outcome_column": outcome_column if outcomes else None, "research_use": RESEARCH_ONLY}, mask_indices)
    if outcomes:
        write_outcome_table(os.path.join(os.path.dirname(tsv), OUTCOMES_TSV), outcomes, outcome_column)
    return tsv


def read_stimulation_table(path, kind="points", outcome_column=None):
    """Stimulation rows of a table: participant_id and x, y, z[, radius] (points) or file (efield / vta, relative to the
    table), plus an optional outcome column. Returns [{participant_id, points | image, outcome, radius_mm}].
    """
    columns, rows = read_table(path)
    if "participant_id" not in columns:
        raise ValueError(f"{os.path.basename(os.fspath(path))} has no participant_id column")
    if outcome_column and outcome_column not in columns:
        raise ValueError(f"{os.path.basename(os.fspath(path))} has no column {outcome_column}")
    folder = os.path.dirname(os.path.abspath(os.fspath(path)))
    out = []
    if kind == "points":
        foci = foci_from_rows(columns, rows, where=os.path.basename(os.fspath(path)))
        for row, focus in zip(rows, foci, strict=True):
            out.append({"participant_id": row["participant_id"], "points": [Focus(*focus.xyz, radius_mm=focus.radius_mm)],
                        "outcome": row.get(outcome_column) if outcome_column else None})
        return out
    file_column = next((name for name in ("file", "path", "image", "efield", "vta") if name in columns), None)
    if file_column is None:
        raise ValueError(f"{os.path.basename(os.fspath(path))} has no file column (the {kind} NIfTI of each row)")
    for row in rows:
        image = row[file_column]
        image = image if os.path.isabs(image) else os.path.join(folder, *image.replace("\\", "/").split("/"))
        out.append({"participant_id": row["participant_id"], "image": image, "outcome": row.get(outcome_column) if outcome_column else None})
    return out


def numeric_columns(path):
    """Columns of a table whose non-empty values are all numbers (outcome candidates), x/y/z/radius excluded."""
    columns, rows = read_table(path)
    skip = {name for key in ("x", "y", "z", "radius", "diameter") for name in _COLUMN_ALIASES[key]} | {"participant_id"}
    out = []
    for column in columns:
        if column in skip:
            continue
        values = [row.get(column) for row in rows if (row.get(column) or "").strip() not in ("", NA)]
        try:
            if values and all(math.isfinite(float(value)) for value in values):
                out.append(column)
        except ValueError:
            continue
    return out


def cohortmaps_arguments(lesions_tsv, bundle_path, out_dir, preview_n=None, preview_seed=0):
    """Arguments of ``python -m LesionNetworkMappingLib.cohortmaps`` that map the seeds of ``lesions_tsv`` (weights
    used as they are: ``--probabilistic``; the job runner adds --threads and --cancel-file).
    """
    args = ["--lesions-tsv", os.fspath(lesions_tsv), "--bundle", os.fspath(bundle_path), "--out", os.fspath(out_dir), "--probabilistic", "--resume"]
    if preview_n:
        args += ["--subjects", str(int(preview_n)), "--random", "--seed", str(int(preview_seed))]
    return args


def null_cost(n_studies, k, n_voxels, columns_per_pass=100):
    """Engine passes of a random-coordinate null: k null sets x n_studies seed columns, ``columns_per_pass`` per pass."""
    per_pass = max(1, int(columns_per_pass) // max(int(n_studies), 1))
    return math.ceil(int(k) / per_pass)


def grid_space(bundle):
    """The template space of a connectome bundle: its metadata "space", else recognized from its grid, else None."""
    metadata = getattr(bundle, "metadata", None) or {}
    if metadata.get("space"):
        return str(metadata["space"])
    from .atlases import mask_family

    return mask_family(bundle.shape, bundle.affine)[1]


#
# Command line
#


def _parser():
    parser = jobs.JsonArgumentParser(prog="python -m LesionNetworkMappingLib.coords", description="Coordinate, atrophy and stimulation seeds; random-coordinate null.")
    commands = parser.add_subparsers(dest="command", required=True)

    def sphere_options(sub):
        group = sub.add_mutually_exclusive_group()
        group.add_argument("--radius", type=float, default=None, help=f"sphere radius in mm (default {DEFAULT_RADIUS_MM:g})")
        group.add_argument("--diameter", type=float, default=None, help="sphere diameter in mm (instead of a radius)")

    seeds = commands.add_parser("seeds", help="study-level union seeds of a coordinate file")
    seeds.add_argument("--foci", required=True, help="CSV/TSV (x, y, z[, radius, study, space]), Sleuth text or NiMARE JSON")
    seeds.add_argument("--bundle", required=True, help="connectome bundle (its grid)")
    seeds.add_argument("--out", required=True, help="run folder (seeds/, lesions.tsv, seeds.json)")
    seeds.add_argument("--space", default="MNI", help="space of foci without one (MNI or TAL)")
    seeds.add_argument("--tal-matrix", choices=tuple(LANCASTER_MATRICES), default=DEFAULT_TAL_MATRIX, help="Lancaster 2007 transform of Talairach foci")
    sphere_options(seeds)
    jobs.add_common_arguments(seeds)

    atrophy = commands.add_parser("atrophy", help="atrophy seeds of per-patient w-maps")
    atrophy.add_argument("--wmap", action="append", required=True, metavar="ID=FILE", help="participant id and w-map NIfTI (repeatable)")
    atrophy.add_argument("--bundle", required=True)
    atrophy.add_argument("--out", required=True)
    atrophy.add_argument("--rule", default=DEFAULT_ATROPHY_RULE, help="w<-2 (default), w<-1.5, w<-2.5 or top5%%")
    atrophy.add_argument("--weighted", action="store_true", help="weight the seed voxels by -w (default: binary)")
    jobs.add_common_arguments(atrophy)

    null = commands.add_parser("null", help="random-coordinate control of a coordinate network map")
    null.add_argument("--foci", required=True)
    null.add_argument("--bundle", required=True)
    null.add_argument("--gm-mask", required=True, help="gray-matter probability map of the connectome's template family (TemplateFlow name)")
    null.add_argument("--gm-space", default=None, help="template space of the GM map (default: from its TemplateFlow file name)")
    null.add_argument("--grid-space", default=None, help="template space of the connectome (default: its metadata or grid)")
    null.add_argument("--out", required=True)
    null.add_argument("--space", default="MNI", help="space of foci without one (MNI or TAL)")
    null.add_argument("--tal-matrix", choices=tuple(LANCASTER_MATRICES), default=DEFAULT_TAL_MATRIX)
    null.add_argument("--k", type=jobs.positive_int, default=DEFAULT_K, help=f"null sets (default {DEFAULT_K})")
    null.add_argument("--seed", type=int, default=0, help="random seed of the null coordinates")
    null.add_argument("--rule", default=None, help="threshold rule JSON of each study's map (default: the Map default)")
    null.add_argument("--sign", choices=("both", "pos", "neg"), default="both")
    null.add_argument("--subjects", type=jobs.positive_int, default=None, help="use N connectome subjects (default: all)")
    null.add_argument("--random", action="store_true", help="choose the N subjects at random")
    null.add_argument("--subject-seed", type=int, default=0)
    null.add_argument("--keep-maps", action="store_true", help="also write the k null overlap maps (4D)")
    sphere_options(null)
    jobs.add_common_arguments(null)
    return parser


def _open_bundle(path):
    from .connectome import ConnectomeBundle

    return ConnectomeBundle.open(path)


def main(argv=None):
    """Command-line entry point (see the module docstring); returns the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(argv)
    try:
        bundle = _open_bundle(args.bundle)
    except (OSError, ValueError, KeyError) as exc:
        raise jobs.JobError(f"The connectome cannot be opened: {exc}", code="input") from None
    jobs.emit("start", job=f"coords-{args.command}", argv=argv, threads=args.threads)
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    try:
        if args.command == "seeds":
            foci = read_foci(args.foci, normalize_space(args.space))
            tsv = prepare_coordinates(out_dir, foci, bundle.grid, args.radius, args.diameter, args.tal_matrix, args.foci, grid_space(bundle),
                                      mask_indices=bundle.mask_indices)
            jobs.emit("done", outputs=[tsv, os.path.join(out_dir, SEEDS_JSON)], elapsed=round(jobs.elapsed(), 3), n=len(group_by_study(foci)))
            return jobs.EXIT_OK
        if args.command == "atrophy":
            wmaps = {}
            for item in args.wmap:
                sid, _sep, path = item.partition("=")
                if not sid or not path:
                    raise jobs.JobError(f"--wmap needs ID=FILE, got {item!r}", code="usage", exit_code=jobs.EXIT_USAGE)
                wmaps[sid] = path
            tsv = prepare_atrophy(out_dir, wmaps, bundle.grid, args.rule, args.weighted, mask_indices=bundle.mask_indices)
            jobs.emit("done", outputs=[tsv, os.path.join(out_dir, SEEDS_JSON)], elapsed=round(jobs.elapsed(), 3), n=len(wmaps))
            return jobs.EXIT_OK
        return _run_null(args, argv, bundle, out_dir)
    except (FamilyError, ValueError, OSError, KeyError) as exc:
        raise jobs.JobError(f"{type(exc).__name__}: {exc}", code="input") from None


def _run_null(args, argv, bundle, out_dir):
    from . import thresholds
    from .export import group_map_path
    from .nifti import write_nifti

    cancel = jobs.CancelToken(args.cancel_file)
    foci, conversion = to_mni(read_foci(args.foci, normalize_space(args.space)), args.tal_matrix)
    groups = group_by_study(foci)
    target = args.grid_space or grid_space(bundle)
    gm = gm_mask_for(args.gm_mask, bundle.grid, args.gm_space, target)
    network_fn, info = engine_network_fn(bundle, thresholds.rule_from_json(args.rule) if args.rule else None, args.sign, args.subjects, args.random, args.subject_seed, cancel)
    from .cohortmaps import batch_size, default_budget_bytes

    columns = batch_size(bundle.n_voxels, default_budget_bytes())
    started = time.monotonic()

    def on_progress(done, total, _elapsed):
        jobs.progress(done, total, started, stage="null sets")

    result = random_coordinate_null(groups, gm, args.k, args.seed, bundle.grid, network_fn, args.radius, args.diameter, bundle.mask_indices,
                                    columns_per_call=columns, keep_maps=args.keep_maps, cancel=cancel, progress=on_progress)
    outputs = {}
    for desc, vector in (("coordOverlapPct", result["observed"]), ("coordNullMeanPct", result["null_mean"]), ("coordNullP", result["voxel_p"])):
        path = group_map_path(out_dir, bundle.name, desc)
        volume = np.zeros(bundle.shape, dtype=np.float32)
        volume.reshape(-1)[bundle.mask_indices] = vector
        write_nifti(path, volume, bundle.affine, dtype=np.float32, descrip=f"LNM {desc}")
        outputs[desc] = os.path.basename(path)
    if args.keep_maps:
        path = group_map_path(out_dir, bundle.name, "coordNullMaps")
        stack = np.zeros((*bundle.shape, result["k"]), dtype=np.float32)
        for draw in range(result["k"]):
            volume = np.zeros(bundle.shape, dtype=np.float32)
            volume.reshape(-1)[bundle.mask_indices] = result["null_maps"][draw]
            stack[..., draw] = volume
        write_nifti(path, stack, bundle.affine, dtype=np.float32, descrip="LNM null overlap maps")
        outputs["coordNullMaps"] = os.path.basename(path)
    with open(os.path.join(out_dir, NULL_PEAKS_TSV), "w", encoding="utf-8", newline="\n") as f:
        f.write("null_set\tpeak_overlap_pct\n" + "".join(f"{i + 1}\t{value:.6g}\n" for i, value in enumerate(result["null_peaks"])))
    record = {"format": FORMAT_NULL, "version": FORMAT_VERSION, "created": _now(), "argv": [os.path.basename(a) if os.path.isabs(a) else a for a in argv],
              "k": result["k"], "seed": result["seed"], "counts": result["counts"], "sphere": result["sphere"], "conversion": conversion,
              "gm_mask": _source(args.gm_mask), "gm_threshold": GM_THRESHOLD, "grid_space": target, "connectome": bundle.name,
              "n_subjects": info["n"], "rule": info["rule"], "statistic": info["statistic"], "sign": info["sign"],
              "observed_peak_pct": result["observed_peak"], "percentile": result["percentile"], "p_peak": result["p_peak"],
              "null_peak_mean_pct": float(np.mean(result["null_peaks"])), "outputs": dict(outputs, peaks=NULL_PEAKS_TSV), "caption": NULL_CAPTION,
              "research_use": RESEARCH_ONLY}
    jobs.write_json_atomic(os.path.join(out_dir, NULL_JSON), record)
    jobs.emit("result", kind="coordinate_null", observed_peak=result["observed_peak"], percentile=result["percentile"], p_peak=result["p_peak"], k=result["k"])
    jobs.emit("done", outputs=[os.path.join(out_dir, NULL_JSON)] + [os.path.join(out_dir, name) for name in outputs.values()], elapsed=round(jobs.elapsed(), 3))
    return jobs.EXIT_OK


if __name__ == "__main__":
    jobs.run_job(main, job="coords")

"""Synthetic study and connectome for the cohort pipeline tests (A-04).

``make_fixture(tmp)`` writes a synthetic connectome bundle with planted networks
("Synthetic connectome.lnmc", 20 x 24 x 20 voxels of 2 mm, 20 subjects) and a study named
"Étude with spaces" whose lesions are on a 1 mm grid covering the bundle's field of view:

- 8 cases near blob A and 4 controls near blob B, with an ``aphasia_score`` column (role score,
  confirmed) and ``volume_ml``; the lesions are BIDS-named files in lesions/, except one case
  stored as "raw scans/patient 7 lesion.nii.gz";
- 5 rows that the pipeline must exclude: include = 0 (sub-13), qc = fail (sub-14), a native-space
  lesion without a scan (sub-15), an empty lesion (sub-16) and a missing lesion file (sub-17).

``make_normalizer_stub(tmp)`` writes a module that mimics A-03's ``normalize batch`` events for the
native row (selected with the environment variable LNM_PIPELINE_NORMALIZER).
"""

import os
import sys
import textwrap

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np  # noqa: E402

from LesionNetworkMappingLib import connectome, nifti  # noqa: E402
from LesionNetworkMappingLib.study import Study  # noqa: E402

STUDY_NAME = "Étude with spaces"
BUNDLE_NAME = "Synthetic connectome.lnmc"
SPACE = "MNI152NLin2009cAsym"
N_CASES = 8
N_CONTROLS = 4
EXCLUDED = {"sub-13": "include=0", "sub-14": "qc=fail", "sub-15": "native", "sub-16": "empty", "sub-17": "missing"}
FOREIGN = "sub-07"


def _fine_grid(grid):
    fine_shape = tuple(2 * int(n) for n in grid.shape)
    fine_affine = np.eye(4)
    fine_affine[:3, 3] = grid.affine[:3, 3] - 0.5
    return fine_shape, fine_affine


def sphere(fine_shape, fine_affine, centre, radius):
    world = np.indices(fine_shape).reshape(3, -1).T @ fine_affine[:3, :3].T + fine_affine[:3, 3]
    return (np.linalg.norm(world - np.asarray(centre, dtype=np.float64), axis=1) <= radius).reshape(fine_shape).astype(np.uint8)


def make_fixture(tmp, n_cases=N_CASES, n_controls=N_CONTROLS, seed=3, extra_rows=True, study_name=STUDY_NAME):
    """Write the bundle and the study under ``tmp``; return {"bundle", "study", "out", "blobs", "sids"}."""
    os.makedirs(tmp, exist_ok=True)
    syn = connectome.make_synthetic(os.path.join(tmp, BUNDLE_NAME), shape=(20, 24, 20), n_subjects=20, runs=(30, 30), seed=1)
    bundle = connectome.ConnectomeBundle.open(syn["path"])
    fine_shape, fine_affine = _fine_grid(bundle.grid)
    study_dir = os.path.join(tmp, study_name)
    study = Study.create(study_dir, name=study_name)
    rng = np.random.default_rng(seed)
    blob_a = np.array(syn["blobs"]["A"]["center_world"])
    blob_b = np.array(syn["blobs"]["B"]["center_world"])
    sids = []
    for index in range(n_cases + n_controls):
        group = "case" if index < n_cases else "control"
        centre = (blob_a if group == "case" else blob_b) + rng.normal(0, 2.0, 3)
        mask = sphere(fine_shape, fine_affine, centre, rng.uniform(3, 6))
        sid = f"sub-{index + 1:02d}"
        if sid == FOREIGN:
            path = os.path.join(study_dir, "raw scans", "patient 7 lesion.nii.gz")
        else:
            path = os.path.join(study_dir, "lesions", f"{sid}_space-{SPACE}_desc-lesion_mask.nii.gz")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        nifti.write_nifti(path, mask, fine_affine)
        score = float(10 - np.linalg.norm(centre - blob_a) / 3 + rng.normal(0, 0.5))
        study.add_subject(sid, {"group": group, "lesion": study.relpath(path), "status": "traced", "space": SPACE, "volume_ml": round(float(mask.sum()) / 1000.0, 4), "aphasia_score": round(score, 4)})
        sids.append(sid)
    if extra_rows:
        start = n_cases + n_controls + 1
        for offset, (sid, kind) in enumerate(EXCLUDED.items()):
            sid = f"sub-{start + offset:02d}"
            path = os.path.join(study_dir, "lesions", f"{sid}_space-{SPACE}_desc-lesion_mask.nii.gz")
            mask = np.zeros(fine_shape, np.uint8)
            if kind != "empty":
                mask[18:22, 20:24, 18:22] = 1
            fields = {"group": "case", "status": "traced", "space": SPACE}
            if kind == "include=0":
                fields["include"] = "0"
            elif kind == "qc=fail":
                fields["qc"] = "fail"
            elif kind == "native":
                fields["space"] = "native"
                path = os.path.join(study_dir, "lesions", f"{sid}_space-native_desc-lesion_mask.nii.gz")
            if kind != "missing":
                nifti.write_nifti(path, mask, fine_affine)
            study.add_subject(sid, {"lesion": study.relpath(path), **fields})
    study.set_column_role("aphasia_score", "score")
    study.save()
    return {"bundle": syn["path"], "study": study_dir, "out": os.path.join(study_dir, "derivatives", "lnm", "conn-Synthetic"), "blobs": syn["blobs"], "sids": sids, "fine_affine": fine_affine,
            "fine_shape": fine_shape}


STUB = '''\
"""Stand-in for ``normalize batch`` (A-03) in the pipeline tests: writes a template-space lesion near blob A and posts it."""
import json
import os
import sys

import numpy as np

from LesionNetworkMappingLib import connectome, nifti
from LesionNetworkMappingLib.study import Study

BUNDLE = {bundle!r}


def emit(event, **fields):
    sys.stdout.write(json.dumps({{"event": event, "t": 0.0, **fields}}) + "\\n")
    sys.stdout.flush()


def main(argv):
    if not argv or argv[0] != "batch":
        emit("error", code="usage", message="batch only")
        return 2
    study_path = argv[argv.index("--study") + 1]
    subjects = argv[argv.index("--subjects") + 1].split(",")
    study = Study.open(study_path)
    bundle = connectome.ConnectomeBundle.open(BUNDLE)
    grid = bundle.grid
    shape = tuple(2 * int(n) for n in grid.shape)
    affine = np.eye(4)
    affine[:3, 3] = grid.affine[:3, 3] - 0.5
    emit("start", job="normalize", pid=os.getpid(), subjects=subjects)
    centre = np.array(connectome.ConnectomeBundle.open(BUNDLE).grid.voxel_to_world(np.array([[5, 6, 10]]))[0])
    world = np.indices(shape).reshape(3, -1).T @ affine[:3, :3].T + affine[:3, 3]
    mask = (np.linalg.norm(world - centre, axis=1) <= 4.5).reshape(shape).astype(np.uint8)
    for index, sid in enumerate(subjects):
        folder = os.path.join(study.path, "derivatives", "normalization", sid)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + "_space-MNI152NLin2009cAsym_res-1_desc-lesion_mask.nii.gz")
        nifti.write_nifti(path, mask, affine)
        values = {{"lesion": study.relpath(path), "space": "MNI152NLin2009cAsym", "norm_method": "stub", "norm_qc": "unreviewed", "lesion_native": study.subject(sid).get("lesion")}}
        inbox = Study.post_result(study.path, sid, values, source="normalize")
        emit("progress", done=index + 1, total=len(subjects))
        emit("result", kind="subject", subject=sid, status="done", out=folder, suggestion="review", inbox=inbox, values=values)
    emit("done", outputs=[], elapsed=0.0, run_id=None)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def make_normalizer_stub(tmp, bundle):
    """Write the normalize batch stand-in; return its path (set LNM_PIPELINE_NORMALIZER to it)."""
    path = os.path.join(tmp, "lnm_normalize_stub.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(STUB.format(bundle=os.path.abspath(bundle))))
    return path

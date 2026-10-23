"""Tests of the streaming tar -> .lnmc converter (connectome.from_tar_stream) and of the connectome catalog
(catalog.py, catalog/connectomes.json).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingTarStreamTest.py -v
"""

import copy
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
import warnings

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np
from scipy import io as sio

from LesionNetworkMappingLib import catalog, connectome, export, fetch, nifti
from LesionNetworkMappingLib.geometry import Grid

GSP_RUN = "sub-{s}/func/sub-{s}_bld00{r}_rest_skip4_stc_mc_bp_0.0001_0.08_resid_FS1mm_MNI1mm_MNI2mm_sm7_finalmask.nii.gz"
SMALL_SHAPE = (9, 11, 8)
SMALL_AFFINE = np.array([[-2.0, 0, 0, 8], [0, 2, 0, -10], [0, 0, 2, -7], [0, 0, 0, 1]])
SMALL_GRID = Grid(SMALL_SHAPE, SMALL_AFFINE)
PAX_TYPE = ord("x")


def _ellipsoid(shape, scale=2.4):
    ijk = np.indices(shape).reshape(3, -1).T
    center = (np.array(shape) - 1) / 2.0
    return ((((ijk - center) / (np.array(shape) / scale)) ** 2).sum(axis=1) <= 1.0).reshape(shape)


def _run(support, n_tr, rng):
    """A 4D float32 run: noise plus an offset inside ``support``, exactly zero outside."""
    data = np.zeros((*support.shape, n_tr), dtype=np.float32)
    data[support] = (100.0 + rng.standard_normal((int(support.sum()), n_tr))).astype(np.float32)
    return data


def _tar(members, fmt=tarfile.PAX_FORMAT):
    """Bytes of an uncompressed tar archive of (name, bytes) members (long names get pax headers)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=fmt) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _offsets(data):
    """{member name: TarInfo.offset} of a tar archive's bytes (read with random access)."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        return {member.name: member.offset for member in tar.getmembers()}


class _Stream:
    """A file object that can only be read in order, like an HTTP response body (no seek, no tell)."""

    def __init__(self, data):
        self._buffer = io.BytesIO(data)

    def read(self, size=-1):
        return self._buffer.read(size)


def _convert(data, out, **kwargs):
    """All checkpoints of a stream conversion of ``data`` (warnings are recorded, not shown)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", connectome.ConversionWarning)
        return list(connectome.from_tar_stream(_Stream(data), out, **kwargs))


class _TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-tarstream-test-")
        self.rng = np.random.default_rng(11)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def write_run(self, name, data, affine=SMALL_AFFINE):
        """Write a run as .nii.gz under the temporary folder; return (its path, its bytes)."""
        path = self.path("runs", os.path.basename(name))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        nifti.write_nifti(path, data, affine, time_step=3.0)
        with open(path, "rb") as f:
            return path, f.read()

    def assertSameBundle(self, first, second, compare_names=True):
        a, b = connectome.ConnectomeBundle.open(first), connectome.ConnectomeBundle.open(second)
        for key in ("shape", "affine", "nVoxels", "subjects", "dtype"):
            self.assertEqual(a.metadata[key], b.metadata[key], key)
        if compare_names:
            self.assertEqual(a.metadata["subjectsSha256"], b.metadata["subjectsSha256"])
        self.assertEqual(a.mask_sha256, b.mask_sha256)
        for subject_id in a.subject_ids:
            sa, sb = a.load_subject(subject_id), b.load_subject(subject_id)
            self.assertTrue(np.array_equal(sa.data, sb.data), subject_id)
            self.assertTrue(np.array_equal(sa.norms, sb.norms), subject_id)


class GspStreamTest(_TempDirTestCase):
    def _gsp_archive(self, grid_shape, affine, runs_per_subject, n_tr=3, junk=True, support=None, extra=None):
        """Members of a GSP-like archive: {subject: n runs}; returns (members, {member name: run path}, support)."""
        support = _ellipsoid(grid_shape) if support is None else support
        members, paths = [], {}
        if junk:
            members += [("._GSP1000_v2_00", b"\0\5\26\7AppleDouble"), (".DS_Store", b"junk"), ("PaxHeader/.DS_Store", b"junk"), ("README", b"text")]
        for subject, n_runs in runs_per_subject.items():
            for run in range(1, n_runs + 1):
                name = GSP_RUN.format(s=subject, r=run)
                data = _run(support if not extra or (subject, run) not in extra else extra[(subject, run)], n_tr + run - 1, self.rng)
                path, raw = self.write_run(name, data, affine)
                members.append((name, raw))
                paths[name] = path
                if junk and run == 1:
                    members.append((f"sub-{subject}/func/._" + os.path.basename(name), b"\0\5\26\7AppleDouble"))
            if junk:
                members += [(f"sub-{subject}/anat/sub-{subject}_T1w.nii.gz", b"not read"), (f"sub-{subject}/vol/sub-{subject}_bld001_surf.nii.gz", b"not read")]
        return members, paths, support

    def _reference(self, paths, support, affine, out):
        mask_path = self.path("support_mask.nii.gz")
        nifti.write_nifti(mask_path, support.astype(np.uint8), affine)
        return connectome.from_nifti_timeseries(sorted(paths.values()), mask_path, out, runs_grouping=r"^(?P<subject>sub-[A-Za-z0-9]+)_")

    def test_gsp_archive_equals_the_nifti_conversion(self):
        # The real GSP run grid (91 x 109 x 91, 2 mm, FSL radiological sform), short runs
        grid = connectome.GSP_RUN_GRID
        members, paths, support = self._gsp_archive(grid.shape, grid.affine, {"0001": 2, "0002": 1}, n_tr=3)
        # sub-0002's run comes first among its members, so the checkpoint after sub-0001 is its pax header
        members = [m for m in members if not m[0].startswith("sub-0002/")] + [m for m in members if m[0].startswith("sub-0002/func/sub")] + [m for m in members if m[0].startswith("sub-0002/") and not m[0].startswith("sub-0002/func/sub")]
        data = _tar(members)
        checkpoints = _convert(data, self.path("stream.lnmc"))
        reference = self._reference(paths, support, grid.affine, self.path("reference.lnmc"))
        self.assertSameBundle(self.path("stream.lnmc"), reference)

        self.assertEqual([c.get("subject") for c in checkpoints], ["sub-0001", "sub-0002", None])
        self.assertEqual([c.get("runs") for c in checkpoints[:2]], [2, 1])
        offsets = _offsets(data)
        first_of_second = GSP_RUN.format(s="0002", r=1)
        self.assertEqual(checkpoints[0]["member_offset"], offsets[first_of_second])
        self.assertEqual(data[offsets[first_of_second] + 156], PAX_TYPE)  # the offset points at the member's pax header
        final = checkpoints[-1]
        self.assertTrue(final["done"] and final["finalized"])
        self.assertEqual((final["subjects"], final["staged"], final["incomplete"]), (2, [], []))
        self.assertEqual(final["member_offset"] % 512, 0)

        bundle = connectome.ConnectomeBundle.open(final["path"])
        mask = bundle.metadata["mask"]
        self.assertEqual(mask["policy"], "first_run_support")
        self.assertEqual(mask["run"], GSP_RUN.format(s="0001", r=1))
        self.assertEqual(mask["sha256"], bundle.mask_sha256)
        self.assertEqual(mask["sha256"], connectome.mask_content_sha256(support))
        self.assertEqual(bundle.n_voxels, int(support.sum()))
        self.assertEqual(bundle.runs("sub-0001"), (3, 4))
        self.assertFalse(os.path.exists(self.path("stream.lnmc", connectome.STAGING_DIR_NAME)))
        self.assertFalse(os.path.exists(self.path("stream.lnmc", connectome.PARTIAL_MANIFEST_NAME)))

    def test_header_and_grid_checks(self):
        members, _, _ = self._gsp_archive(SMALL_SHAPE, SMALL_AFFINE, {"0001": 1}, junk=False)
        data = _tar(members)
        with self.assertRaisesRegex(ValueError, "not on the expected grid"):
            _convert(data, self.path("default-grid.lnmc"))  # the default grid is GSP's 91 x 109 x 91
        self.assertEqual(len(_convert(data, self.path("any-grid.lnmc"), grid=False)), 2)
        shifted = SMALL_AFFINE.copy()
        shifted[0, 3] += 2.0
        other, _, _ = self._gsp_archive(SMALL_SHAPE, shifted, {"0002": 1}, junk=False)
        with self.assertRaisesRegex(ValueError, "not on the grid of the connectome mask"):
            _convert(_tar(members + other), self.path("two-grids.lnmc"), grid=False)
        _, flat = self.write_run("flat.nii.gz", np.ones(SMALL_SHAPE, dtype=np.float32))
        with self.assertRaisesRegex(ValueError, "4D time series"):
            _convert(_tar([(GSP_RUN.format(s="0003", r=1), flat)]), self.path("3d.lnmc"), grid=SMALL_GRID)

    def test_resume_from_a_member_offset_reproduces_the_rest(self):
        members, _, _ = self._gsp_archive(SMALL_SHAPE, SMALL_AFFINE, {"0001": 2, "0002": 1, "0003": 2})
        data = _tar(members)
        full = _convert(data, self.path("full.lnmc"), grid=SMALL_GRID)

        cancel = threading.Event()
        partial = []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", connectome.ConversionWarning)
            for checkpoint in connectome.from_tar_stream(io.BytesIO(data), self.path("resumed.lnmc"), grid=SMALL_GRID, cancel=cancel):
                partial.append(checkpoint)
                cancel.set()  # stop after the first subject
        self.assertEqual(partial[0]["subject"], "sub-0001")
        self.assertTrue(partial[-1]["cancelled"])
        offset = partial[-1]["member_offset"]
        self.assertEqual(offset, partial[0]["member_offset"])  # nothing of sub-0002 was kept
        self.assertFalse(os.path.exists(self.path("resumed.lnmc", connectome.MANIFEST_NAME)))

        # A new stream that starts at that byte of the archive (as an HTTP Range request would)
        rest = _convert(data[offset:], self.path("resumed.lnmc"), grid=SMALL_GRID, start_offset=offset)
        self.assertEqual([c.get("subject") for c in rest], ["sub-0002", "sub-0003", None])
        self.assertEqual([c["member_offset"] for c in rest], [c["member_offset"] for c in full[1:]])
        self.assertSameBundle(self.path("full.lnmc"), self.path("resumed.lnmc"))
        self.assertEqual(connectome.ConnectomeBundle.open(self.path("resumed.lnmc")).metadata["mask"]["policy"], "first_run_support")

        # The mask does not depend on where the stream starts: resuming keeps the bundle's mask
        again = _convert(data, self.path("resumed.lnmc"), grid=SMALL_GRID)
        self.assertEqual([c.get("skipped") for c in again[:-1]], [True, True, True])
        self.assertSameBundle(self.path("full.lnmc"), self.path("resumed.lnmc"))
        with self.assertRaises(FileExistsError):
            _convert(data, self.path("resumed.lnmc"), grid=SMALL_GRID, resume=False)

    def test_gnu_long_names(self):
        members, _, _ = self._gsp_archive(SMALL_SHAPE, SMALL_AFFINE, {"0001": 1, "0002": 1})
        data = _tar(members, fmt=tarfile.GNU_FORMAT)
        full = _convert(data, self.path("gnu.lnmc"), grid=SMALL_GRID)
        offset = full[0]["member_offset"]
        self.assertEqual(offset, _offsets(data)[GSP_RUN.format(s="0002", r=1)])
        self.assertEqual(data[offset + 156], ord("L"))  # the GNU long-name header of the member
        rest = _convert(data[offset:], self.path("gnu-rest.lnmc"), grid=SMALL_GRID, start_offset=offset)
        self.assertEqual([c.get("subject") for c in rest], ["sub-0002", None])

    def test_subject_split_over_two_archives(self):
        members, paths, support = self._gsp_archive(SMALL_SHAPE, SMALL_AFFINE, {"0001": 2, "0002": 2, "0003": 1})
        split = next(i for i, m in enumerate(members) if m[0] == GSP_RUN.format(s="0002", r=2))
        first, second = _tar(members[:split]), _tar(members[split:])
        checkpoints = _convert(first, self.path("split.lnmc"), grid=SMALL_GRID, finalize=False)
        self.assertEqual(checkpoints[-1]["staged"], ["sub-0002"])
        self.assertIsNone(checkpoints[-1]["path"])
        self.assertTrue(os.path.isdir(self.path("split.lnmc", connectome.STAGING_DIR_NAME, "sub-0002")))
        self.assertFalse(os.path.exists(self.path("split.lnmc", connectome.MANIFEST_NAME)))
        checkpoints = _convert(second, self.path("split.lnmc"), grid=SMALL_GRID)
        self.assertEqual([c.get("subject") for c in checkpoints], ["sub-0002", "sub-0003", None])
        self.assertEqual(checkpoints[0]["runs"], 2)
        self.assertSameBundle(self.path("split.lnmc"), self._reference(paths, support, SMALL_AFFINE, self.path("reference.lnmc")))
        self.assertFalse(os.path.exists(self.path("split.lnmc", connectome.STAGING_DIR_NAME)))

    def test_damaged_members_and_truncated_archives_raise(self):
        support = _ellipsoid(SMALL_SHAPE)
        _, raw = self.write_run("run.nii.gz", _run(support, 4, self.rng))
        name = GSP_RUN.format(s="0001", r=1)

        def damaged(change):
            data = bytearray(raw)
            change(data)
            return _tar([(name, bytes(data))])

        def flip_crc(data):
            data[-8] ^= 0xFF

        def flip_size(data):
            data[-1] ^= 0x01

        def flip_body(data):
            data[len(data) // 2] ^= 0xFF

        with self.assertRaisesRegex(connectome.StreamIntegrityError, "CRC32"):
            _convert(damaged(flip_crc), self.path("crc.lnmc"), grid=SMALL_GRID)
        with self.assertRaisesRegex(connectome.StreamIntegrityError, "ISIZE"):
            _convert(damaged(flip_size), self.path("isize.lnmc"), grid=SMALL_GRID)
        with self.assertRaises(connectome.StreamIntegrityError):
            _convert(damaged(flip_body), self.path("body.lnmc"), grid=SMALL_GRID)
        with self.assertRaisesRegex(connectome.StreamIntegrityError, "truncated"):
            _convert(_tar([(name, raw[: len(raw) - 20])]), self.path("short.lnmc"), grid=SMALL_GRID)
        with self.assertRaisesRegex(connectome.StreamIntegrityError, "not a gzip"):
            _convert(_tar([(name, b"plain text, not gzip data")]), self.path("plain.lnmc"), grid=SMALL_GRID)
        # Concatenated gzip members are one file (RFC 1952); a damaged later member is still caught
        uncompressed = gzip.decompress(raw)
        two = gzip.compress(uncompressed[:1000], mtime=0) + gzip.compress(uncompressed[1000:], mtime=0)
        self.assertEqual(len(_convert(_tar([(name, two)]), self.path("two.lnmc"), grid=SMALL_GRID)), 2)
        two = bytearray(two)
        two[-8] ^= 0xFF
        with self.assertRaisesRegex(connectome.StreamIntegrityError, "CRC32"):
            _convert(_tar([(name, bytes(two))]), self.path("two-bad.lnmc"), grid=SMALL_GRID)

        # A download cut in a member, or between members (no end-of-archive marker), raises after the
        # subjects that were complete: the conversion resumes from the last checkpoint
        members, _, _ = self._gsp_archive(SMALL_SHAPE, SMALL_AFFINE, {"0001": 1, "0002": 1}, junk=False)
        data = _tar(members)
        offsets = _offsets(data)
        seen = []
        with self.assertRaises(connectome.StreamIntegrityError):
            for checkpoint in connectome.from_tar_stream(io.BytesIO(data[: offsets[members[1][0]] + 2000]), self.path("cut.lnmc"), grid=SMALL_GRID):
                seen.append(checkpoint)
        self.assertEqual([c["subject"] for c in seen], ["sub-0001"])
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            tar.getmembers()
            end = tar.offset  # where the end-of-archive marker starts
        self.assertEqual(data[end:], b"\0" * len(data[end:]))
        with self.assertRaisesRegex(connectome.StreamIntegrityError, "end-of-archive"):
            _convert(data[:end], self.path("no-end.lnmc"), grid=SMALL_GRID)

    def test_support_mismatch_warns_and_the_mask_stays_fixed(self):
        support = _ellipsoid(SMALL_SHAPE)
        changed = support.copy()
        inside = tuple(np.argwhere(support)[0])
        outside = tuple(np.argwhere(~support)[0])
        changed[inside] = False
        changed[outside] = True
        members, _, _ = self._gsp_archive(SMALL_SHAPE, SMALL_AFFINE, {"0001": 1, "0002": 1}, support=support, extra={("0002", 1): changed})
        with self.assertWarns(connectome.SupportMismatchWarning) as caught:
            checkpoints = list(connectome.from_tar_stream(io.BytesIO(_tar(members)), self.path("mismatch.lnmc"), grid=SMALL_GRID))
        self.assertIn("1 voxel(s) outside the mask", str(caught.warning))
        self.assertEqual(checkpoints[0]["warnings"], [])
        self.assertEqual(len(checkpoints[1]["warnings"]), 1)
        self.assertEqual(len(checkpoints[-1]["warnings"]), 1)
        bundle = connectome.ConnectomeBundle.open(checkpoints[-1]["path"])
        self.assertTrue(np.array_equal(bundle.mask, support))
        row = list(np.flatnonzero(support.reshape(-1))).index(np.ravel_multi_index(inside, SMALL_SHAPE))
        self.assertFalse(bundle.load_subject("sub-0002").data[row].any())  # zero in that run: zero-variance row

    def test_explicit_mask_file(self):
        support = _ellipsoid(SMALL_SHAPE)
        smaller = _ellipsoid(SMALL_SHAPE, scale=3.0)
        mask_path = self.path("explicit_mask.nii.gz")
        nifti.write_nifti(mask_path, smaller.astype(np.uint8), SMALL_AFFINE)
        members, _, _ = self._gsp_archive(SMALL_SHAPE, SMALL_AFFINE, {"0001": 1, "0002": 1}, support=support)
        with self.assertWarns(connectome.SupportMismatchWarning):
            checkpoints = list(connectome.from_tar_stream(io.BytesIO(_tar(members)), self.path("file.lnmc"), mask_policy="file", mask_path=mask_path, grid=SMALL_GRID))
        bundle = connectome.ConnectomeBundle.open(checkpoints[-1]["path"])
        self.assertEqual(bundle.n_voxels, int(smaller.sum()))
        self.assertEqual(bundle.metadata["mask"]["policy"], "file")
        self.assertEqual(bundle.metadata["mask"]["source"], "file: explicit_mask.nii.gz")  # a name, never a local path
        self.assertEqual(bundle.metadata["mask"]["sha256"], connectome.mask_content_sha256(smaller))
        with self.assertRaisesRegex(ValueError, "mask_path"):
            connectome.from_tar_stream(io.BytesIO(b""), self.path("x.lnmc"), mask_policy="file")
        with self.assertRaisesRegex(ValueError, "layout"):
            connectome.from_tar_stream(io.BytesIO(b""), self.path("x.lnmc"), layout="zip")

    def test_provenance_and_cancel_while_reading(self):
        members, _, _ = self._gsp_archive(SMALL_SHAPE, SMALL_AFFINE, {"0001": 1})
        provenance = {"licence": "GSP-DUT-2014", "source": {"doi": "10.7910/DVN/ILXIKS", "version": "3.0"}, "preprocessing": {"gsr": True, "smoothingMm": 7}, "trS": 3.0}
        progress = []
        checkpoints = _convert(_tar(members), self.path("tagged.lnmc"), grid=SMALL_GRID, provenance=provenance, name="GSP1000 test", progress=lambda *a: progress.append(a))
        bundle = connectome.ConnectomeBundle.open(checkpoints[-1]["path"])
        self.assertEqual((bundle.name, bundle.provenance["licence"], bundle.provenance["source"]["doi"], bundle.provenance["trS"]), ("GSP1000 test", "GSP-DUT-2014", "10.7910/DVN/ILXIKS", 3.0))
        self.assertEqual([p[:2] for p in progress], [(1, None)])
        cancelled = _convert(_tar(members), self.path("cancelled.lnmc"), grid=SMALL_GRID, cancel=lambda: True)
        self.assertEqual(len(cancelled), 1)
        self.assertTrue(cancelled[0]["cancelled"])
        self.assertEqual(cancelled[0]["member_offset"], 0)


class LeadDbsStreamTest(_TempDirTestCase):
    def _dataset(self):
        """A Lead-DBS dataset folder (dataset_volsurf.mat, dataset_info.json, vol/*.mat): subB has two runs."""
        shape = (5, 6, 4)
        mask = np.zeros(shape, dtype=bool)
        mask[1:4, 1:5, 1:3] = True
        outidx = np.sort(np.ravel_multi_index(np.nonzero(mask), shape, order="F")) + 1
        dataset = self.path("GSPtest")
        os.makedirs(os.path.join(dataset, "vol"))
        runs = {"subA": ["subA_run1.mat"], "subB": ["subB_run1.mat", "subB_run2.mat"], "subC": ["subC_run1.mat"]}
        sub_ids = np.empty((3, 1), dtype=object)
        for index, (subject, files) in enumerate(runs.items()):
            sub_ids[index, 0] = np.array([subject, *files], dtype=object)
            for name in files:
                sio.savemat(os.path.join(dataset, "vol", name), {"gmtc": (50 + self.rng.standard_normal((10, len(outidx)))).astype(np.float32)})
        spm_mat = np.array([[-2.0, 0, 0, 92], [0, 2, 0, -128], [0, 0, 2, -74], [0, 0, 0, 1]])
        vol = {"outidx": outidx.astype(np.float64)[:, np.newaxis], "space": {"dim": np.array([shape], np.float64), "mat": spm_mat, "fname": "space.nii"}, "subIDs": sub_ids}
        sio.savemat(os.path.join(dataset, "dataset_volsurf.mat"), {"vol": vol})
        info = {"name": "GSP 1000 v2 (Cohen 2023)", "tag": "GSP1000v2", "type": "fMRI_timecourses", "citation": "Cohen 2023 https://doi.org/10.7910/DVN/KKTJQC",
                "subsets": [{"name": "Full Set", "subs": [1, 2, 3]}, {"name": "First Subject", "subs": 1}, {"name": "First 2 Subjects", "subs": [1, 2]}]}
        with open(os.path.join(dataset, "dataset_info.json"), "w", encoding="utf-8") as f:
            json.dump(info, f)
        return dataset, runs

    def _member(self, dataset, name):
        with open(os.path.join(dataset, "vol", name), "rb") as f:
            return (f"GSP1000-LeadDBS/vol/{name}", f.read())

    def test_streamed_archives_equal_from_leaddbs(self):
        dataset, _ = self._dataset()
        reference = connectome.from_leaddbs(dataset, self.path("reference.lnmc"))
        # Archive 1 holds subA and the first run of subB, archive 2 the rest (subB is split over both)
        first = _tar([("GSP1000-LeadDBS/vol/._subA_run1.mat", b"junk"), self._member(dataset, "subA_run1.mat"), self._member(dataset, "subB_run1.mat"), ("GSP1000-LeadDBS/vol/unrelated.mat", b"x")])
        second = _tar([self._member(dataset, "subB_run2.mat"), self._member(dataset, "subC_run1.mat")])
        out = self.path("stream.lnmc")
        checkpoints = _convert(first, out, layout="leaddbs_vol", dataset_dir=dataset, finalize=False)
        self.assertEqual([c.get("subject") for c in checkpoints], ["subA", None])
        self.assertEqual(checkpoints[-1]["staged"], ["subB"])
        checkpoints = _convert(second, out, layout="leaddbs_vol", dataset_dir=dataset)
        self.assertEqual([c.get("subject") for c in checkpoints], ["subB", "subC", None])
        self.assertSameBundle(out, reference)
        bundle = connectome.ConnectomeBundle.open(out)
        self.assertEqual(bundle.subsets, connectome.ConnectomeBundle.open(reference).subsets)
        self.assertEqual(bundle.metadata["mask"]["policy"], "volsurf")
        self.assertEqual(bundle.provenance["source"]["doi"], "10.7910/DVN/KKTJQC")
        self.assertIn("Cohen 2023", bundle.citation)

        # Only the first archive, finalized: subB is incomplete and the subsets keep only converted subjects
        partial = _convert(first, self.path("first-only.lnmc"), layout="leaddbs_vol", dataset_dir=dataset)
        self.assertEqual(partial[-1]["incomplete"], ["subB"])
        self.assertEqual(connectome.ConnectomeBundle.open(partial[-1]["path"]).subsets, {"First Subject": ["subA"]})
        with self.assertRaisesRegex(ValueError, "dataset_dir"):
            connectome.from_tar_stream(io.BytesIO(first), self.path("x.lnmc"), layout="leaddbs_vol")

    def test_matlab_v73_member_needs_h5py(self):
        buffer = io.BytesIO(b"MATLAB 7.3 MAT-file, Platform: GLNXA64" + b"\0" * 600)
        buffer.name = "GSP1000-LeadDBS/vol/subA_run1.mat"
        with self.assertRaisesRegex(connectome.UnsupportedFormatError, "subA_run1.mat is a MATLAB v7.3"):
            connectome._loadmat(buffer)
        self.assertEqual(buffer.tell(), 0)


class ConnectomeCatalogTest(unittest.TestCase):
    EXPECTED = ("gsp1000-ilxiks", "gsp1000-kktjqc", "ukb-migp-grouppca", "leaddbs-ppmi-fgroup2017", "leaddbs-torpd", "leaddbs-pediatric", "synthetic-demo")

    def setUp(self):
        self.catalog = catalog.load_catalog()

    def test_catalog_validates(self):
        self.assertEqual(self.catalog.data["format"], "slicer-lnm-catalog")
        self.assertEqual(self.catalog.data["version"], 1)
        self.assertEqual(tuple(self.catalog.ids()), self.EXPECTED)
        self.assertEqual(catalog.validate_catalog(self.catalog.data), [])
        for entry in self.catalog:
            self.assertEqual(catalog.validate_entry(entry), [], entry["id"])
            for item in entry["files"]:
                if isinstance(item.get("checksum"), dict):
                    self.assertNotIn("-", item["checksum"]["value"])  # never a multipart S3 ETag
        self.assertNotIn("connectomes", fetch.CATALOG_NAMES)  # the fetch layer never lists connectomes

    def test_seed_entries(self):
        ilxiks = self.catalog.get("gsp1000-ilxiks")
        self.assertEqual((ilxiks["source"]["doi"], ilxiks["source"]["version"], ilxiks["size"]), ("10.7910/DVN/ILXIKS", "3.0", 235_963_445_564))
        self.assertEqual(len(catalog.archives(ilxiks)), 20)
        self.assertEqual(catalog.archives(ilxiks)[16]["name"], "GSP1000_v2_16.tar")
        self.assertEqual((ilxiks["licence"], ilxiks["terms"]["fetch"], ilxiks["terms"]["field"]), ("GSP-DUT-2014", "live", "data.latestVersion.termsOfUse"))
        self.assertEqual((ilxiks["space"], ilxiks["grid"], ilxiks["resolution_mm"]), ("MNI152NLin6Asym", "NLin6-2mm", 2))
        self.assertEqual((ilxiks["population"]["n"], ilxiks["population"]["age_range"], ilxiks["tr_s"]), (1000, [18, 36], 3.0))
        self.assertEqual((ilxiks["preprocessing"]["gsr"], ilxiks["preprocessing"]["smoothing_mm"]), (True, 7))
        self.assertTrue(ilxiks["preprocessing"]["pipeline_url"].startswith("https://github.com/"))
        self.assertEqual(ilxiks["converter"], "gsp_ilxiks")
        self.assertEqual(ilxiks["acknowledgement"], export.GSP_ACKNOWLEDGEMENT)
        self.assertNotIn("subsets", ilxiks)  # an ILXIKS archive subset is not a Lead-DBS subset

        kktjqc = self.catalog.get("gsp1000-kktjqc")
        self.assertEqual((kktjqc["source"]["version"], kktjqc["size"], len(catalog.archives(kktjqc))), ("4.0", 218_932_842_997, 35))
        self.assertEqual((kktjqc["converter"], kktjqc["subsets"]["First 100 Subjects"]), ("leaddbs_vol", 100))
        self.assertEqual({f["name"] for f in kktjqc["files"] if f["role"] == "metadata"}, {"dataset_info.json", "dataset_info.mat", "dataset_volsurf.mat"})

        ukb = self.catalog.get("ukb-migp-grouppca")
        self.assertEqual((ukb["licence"], ukb["size"], ukb["kind"], ukb["status"]), ("CC-BY-4.0", 1_199_307_960, "group_pca", "unsupported"))
        self.assertIn("Not supported in this version", ukb["status_note"])
        self.assertEqual(self.catalog.get("synthetic-demo")["status"], "local")

    def test_badges_and_link_out(self):
        badges = {entry["id"]: catalog.licence_badge(entry) for entry in self.catalog}
        self.assertEqual(badges, {"gsp1000-ilxiks": "amber", "gsp1000-kktjqc": "amber", "ukb-migp-grouppca": "green", "leaddbs-ppmi-fgroup2017": "grey", "leaddbs-torpd": "grey", "leaddbs-pediatric": "grey", "synthetic-demo": "green"})
        self.assertEqual({entry["id"] for entry in self.catalog if catalog.link_out(entry)}, {"leaddbs-ppmi-fgroup2017", "leaddbs-torpd", "leaddbs-pediatric"})
        for entry in self.catalog:
            self.assertEqual(entry["badge"], catalog.licence_badge(entry))
        self.assertEqual(catalog.licence_badge({"licence": "CC0-1.0"}), "green")
        self.assertEqual(catalog.licence_badge({"licence": "CC-BY-NC-4.0"}), "amber")
        self.assertTrue(catalog.link_out({"licence": "unknown"}))  # terms not stated: never accepted automatically
        self.assertFalse(catalog.link_out({"licence": "GSP-DUT-2014"}))

    def test_terms_sha256(self):
        text = "1. I will not attempt to establish the identity of subjects.\n2. I will acknowledge GSP.\n"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.assertEqual(catalog.terms_sha256(text), expected)
        self.assertEqual(catalog.terms_sha256(text.replace("\n", "\r\n")), expected)
        self.assertEqual(catalog.terms_sha256(text.replace("\n", "\r")), expected)
        self.assertNotEqual(catalog.terms_sha256(text + " "), expected)
        self.assertEqual(catalog.terms_sha256("Ä"), hashlib.sha256("Ä".encode()).hexdigest())  # UTF-8
        with self.assertRaises(TypeError):
            catalog.terms_sha256(None)

    def test_disk_plan_arithmetic(self):
        ilxiks = self.catalog.get("gsp1000-ilxiks")
        archives = catalog.archives(ilxiks)
        others = sum(f["size"] for f in ilxiks["files"] if f["role"] != "archive")
        plan = catalog.disk_plan(ilxiks)
        self.assertEqual(plan["download"], 235_963_445_564)
        self.assertEqual(plan["stored_range"], [126_000_000_000, 140_000_000_000])
        staging = 292_019 * 120 * 4 * 2
        self.assertEqual(plan["staging"], staging)
        self.assertEqual(plan["peak"], 140_000_000_000 + others + staging)  # streamed: no archive on disk
        self.assertTrue(plan["exact"])
        keep = catalog.disk_plan(ilxiks, keep_archives=True)
        self.assertEqual(keep["peak"], plan["peak"] + 235_963_445_564 - others)

        two = catalog.disk_plan(ilxiks, "archives:00-01")
        two_bytes = archives[0]["size"] + archives[1]["size"]
        self.assertEqual(two["archives"], ["GSP1000_v2_00.tar", "GSP1000_v2_01.tar"])
        self.assertEqual(two["download"], two_bytes + others)
        fraction = two_bytes / sum(f["size"] for f in archives)
        self.assertEqual(two["stored_range"], [round(126e9 * fraction), round(140e9 * fraction)])
        self.assertEqual(two["subjects"], round(1000 * fraction))
        self.assertEqual(catalog.disk_plan(ilxiks, "archives:0,1")["download"], two["download"])

        hundred = catalog.disk_plan(ilxiks, 100)
        self.assertEqual(hundred["archives"], ["GSP1000_v2_00.tar", "GSP1000_v2_01.tar"])  # ceil(100 / 1000 * 20)
        self.assertEqual(hundred["stored_range"], [12_600_000_000, 14_000_000_000])
        self.assertFalse(hundred["exact"])
        self.assertEqual(catalog.disk_plan(ilxiks, "subjects:100")["download"], hundred["download"])
        kktjqc = self.catalog.get("gsp1000-kktjqc")
        first100 = catalog.disk_plan(kktjqc, "First 100 Subjects")
        self.assertEqual(len(first100["archives"]), 4)  # ceil(100 / 1000 * 35)
        self.assertIsNone(first100["stored"])  # the Lead-DBS mask is known only from dataset_volsurf.mat
        for bad in ("archives:20", "archives:03-01", "first fifty", 0):
            with self.assertRaises(ValueError):
                catalog.disk_plan(ilxiks, bad)

        demo = catalog.disk_plan(self.catalog.get("synthetic-demo"))
        self.assertEqual((demo["download"], demo["stored"], demo["peak"]), (0, 6360 * (20 * 2 + 4) * 2 * 120, 6360 * (20 * 2 + 4) * 2 * 120))
        ukb = catalog.disk_plan(self.catalog.get("ukb-migp-grouppca"))
        self.assertEqual((ukb["download"], ukb["peak"]), (1_199_307_960, 1_199_307_960 + 700_000_000))
        torpd = catalog.disk_plan(self.catalog.get("leaddbs-torpd"))
        self.assertEqual((torpd["download"], torpd["peak"]), (25_665_221_855, None))

    def test_validation_finds_problems(self):
        entry = copy.deepcopy(self.catalog.get("leaddbs-torpd"))
        entry["link_out"] = False
        entry["status"] = "supported"
        problems = catalog.validate_entry(entry)
        self.assertTrue(any("link-out only" in p for p in problems), problems)
        entry = copy.deepcopy(self.catalog.get("gsp1000-ilxiks"))
        entry["badge"] = "green"
        entry["files"][0]["checksum"] = {"algo": "md5", "value": "0123456789abcdef0123456789abcdef-172"}
        entry["size"] += 1
        entry["subsets"] = {"First 100 Subjects": 100}
        problems = catalog.validate_entry(entry)
        for text in ("badge green does not match", "multipart S3 ETag", "not the sum of the file sizes", "named subsets exist only"):
            self.assertTrue(any(text in p for p in problems), (text, problems))
        entry = copy.deepcopy(self.catalog.get("ukb-migp-grouppca"))
        del entry["status_note"]
        entry["terms"] = {"fetch": "live", "url": "https://example.org"}
        problems = catalog.validate_entry(entry)
        self.assertTrue(any("status_note" in p for p in problems) and any("live terms" in p for p in problems), problems)

        path = os.path.join(tempfile.mkdtemp(prefix="lnm-catalog-test-"), "broken.json")
        try:
            data = copy.deepcopy(self.catalog.data)
            data["entries"].append(copy.deepcopy(data["entries"][0]))
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            with self.assertRaises(catalog.CatalogError) as caught:
                catalog.load_catalog(path)
            self.assertTrue(any("duplicate id" in p for p in caught.exception.problems))
        finally:
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

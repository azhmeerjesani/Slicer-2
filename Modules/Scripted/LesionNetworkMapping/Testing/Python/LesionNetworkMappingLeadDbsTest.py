"""Tests of the Lead-DBS interoperability (leaddbs.py, catalog/parity_leaddbs.json; addendum F19).

Offline: Lead-DBS-named outputs are synthetic NIfTI files, the parity reference files are adopted into a
temporary download cache, and the parity run maps lesions with a small synthetic connectome.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingLeadDbsTest.py -v
"""

import contextlib
import gzip
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import cohort, cohortmaps, connectome, engine, export, fetch, glm, jobs, leaddbs, nifti, reference, runs
from LesionNetworkMappingLib.design import DesignSpec
from LesionNetworkMappingLib.study import Study

_SAVED_ENV = {}


def setUpModule():
    _SAVED_ENV[fetch.OFFLINE_ENV] = os.environ.get(fetch.OFFLINE_ENV)
    os.environ[fetch.OFFLINE_ENV] = "1"  # these tests never use the network


def tearDownModule():
    value = _SAVED_ENV.pop(fetch.OFFLINE_ENV)
    if value is None:
        os.environ.pop(fetch.OFFLINE_ENV, None)
    else:
        os.environ[fetch.OFFLINE_ENV] = value


def _run_job(main, argv, job):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = jobs.run_job(main, argv, job=job, exit=False, orphan=False, signals=False)
    jobs.reset_cancel()
    return code, [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]


class _TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm-leaddbs-test-")
        self.rng = np.random.default_rng(34)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


class NamingTest(_TempCase):
    def test_labels(self):
        kktjqc = {"name": "GSP 1000 v2 (Cohen 2023)", "source": {"tag": "GSP1000v2", "doi": "10.7910/DVN/KKTJQC"}}
        self.assertEqual(leaddbs.leaddbs_label(kktjqc), "GSP1000v2xFullSet")
        self.assertEqual(leaddbs.leaddbs_label(kktjqc, "Full Set"), "GSP1000v2xFullSet")
        self.assertEqual(leaddbs.leaddbs_label(kktjqc, "First 100 Subjects"), "GSP1000v2xFirst100Subjects")
        self.assertEqual(leaddbs.leaddbs_label(kktjqc, "user:my set"), "GSP1000v2xmyset")
        self.assertEqual(leaddbs.leaddbs_label("GSP1000 ILXIKS v3.0"), "GSP1000ILXIKSv30xFullSet")  # no tag: the name
        self.assertTrue(leaddbs.is_leaddbs_release(kktjqc))
        self.assertTrue(leaddbs.is_leaddbs_release({"name": "x", "source": {"tag": "GSP1000v2"}}))
        self.assertFalse(leaddbs.is_leaddbs_release({"name": "GSP1000", "source": {"doi": "10.7910/DVN/ILXIKS"}}))
        self.assertEqual(leaddbs.infer_n("GSP1000v2xFullSet"), 1000)
        self.assertEqual(leaddbs.infer_n("GSP1000v2xFirst100Subjects"), 100)
        self.assertEqual(leaddbs.infer_n("GSP1000v2xFirstSubject"), 1)
        self.assertIsNone(leaddbs.infer_n("MyConnectome"))

    def test_bundle_label_and_paths(self):
        made = connectome.make_synthetic(self.path("b.lnmc"), shape=(8, 9, 8), n_subjects=2, runs=(12,), name="Synthetic Demo")
        bundle = connectome.ConnectomeBundle.open(made["path"])
        self.assertEqual(leaddbs.leaddbs_label(bundle), "SyntheticDemoxFullSet")
        self.assertFalse(leaddbs.is_leaddbs_release(bundle))
        lead = leaddbs.map_paths("/o", "sub-01", bundle, "First 5 Subjects", style="leaddbs")
        self.assertEqual(os.path.basename(lead["T"]), "sub-01_conn-SyntheticDemoxFirst5Subjects_desc-T_funcmap.nii.gz")
        self.assertEqual(os.path.basename(lead["AvgFz"]), "sub-01_conn-SyntheticDemoxFirst5Subjects_desc-AvgRFz_funcmap.nii.gz")
        slicer_style = leaddbs.map_paths("/o", "sub-01", bundle, style="slicer")
        self.assertEqual(slicer_style, export.map_paths("/o", "sub-01", bundle.name))
        with self.assertRaises(ValueError):
            leaddbs.map_paths("/o", "sub-01", bundle, style="other")

    def test_parse_names(self):
        info = leaddbs.parse_funcmap_name("sub_M2001_lesion.nii_conn-GSP1000v2xFullSet_desc-T_funcmap.nii.gz")
        self.assertEqual((info["lesion"], info["conn"], info["key"]), ("sub_M2001_lesion", "GSP1000v2xFullSet", "T"))
        info = leaddbs.parse_funcmap_name("lesion1_EPILEPSY_JI_ONLYNEG_111_exp6mm.nii.gz_MNI152_222.nii_conn-GSP1000v2xFullSet_desc-AvgR_funcmap.nii.gz")
        self.assertEqual((info["lesion"], info["key"]), ("lesion1_EPILEPSY_JI_ONLYNEG_111_exp6mm.nii.gz_MNI152_222", "AvgR"))
        self.assertEqual(leaddbs.parse_funcmap_name("p_conn-X_desc-AvgR_Fz_funcmap.nii")["key"], "AvgFz")
        self.assertEqual(leaddbs.parse_funcmap_name("p_conn-X_desc-AvgRFz_funcmap.nii.gz")["key"], "AvgFz")
        self.assertIsNone(leaddbs.parse_funcmap_name("p_conn-X_desc-PFwe_map.nii.gz"))
        self.assertIsNone(leaddbs.parse_funcmap_name("notes.txt"))
        mapping, unmatched = leaddbs.match_participants(["sub_M2001_lesion", "0675_lesion_MNI", "sub-07_ses-1"], ["sub-M2001", "sub-0675"])
        self.assertEqual(mapping, {"sub_M2001_lesion": "sub-M2001", "0675_lesion_MNI": "sub-0675", "sub-07_ses-1": "sub-07"})
        self.assertEqual(unmatched, ["sub-07_ses-1"])
        self.assertEqual(leaddbs.derived_participant_id("r148_lesion_MNI"), "sub-r148lesionMNI")


def _load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(path, data, affine):
    nifti.write_nifti(path, data, affine)
    return path


def _write_map(path, data, affine, compressed=True):
    if compressed:
        nifti.write_nifti(path, data.astype(np.float32), affine)
    else:
        tmp = path + ".gz"
        nifti.write_nifti(tmp, data.astype(np.float32), affine)
        with gzip.open(tmp, "rb") as src, open(path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.remove(tmp)
    return path


class ImportTest(_TempCase):
    SHAPE = (7, 8, 6)
    AFFINE = np.array([[-2.0, 0, 0, 12.0], [0, 2.0, 0, -14.0], [0, 0, 2.0, -8.0], [0, 0, 0, 1]])

    def make_outputs(self, folder, ids, label="GSP1000v2xFullSet", fz=True):
        """Lead-DBS-style outputs (some .nii, the aphasia "sub_<id>_lesion.nii_conn-" names); returns the T arrays."""
        os.makedirs(folder, exist_ok=True)
        brain = np.zeros(self.SHAPE, bool)
        brain[1:-1, 1:-1, 1:-1] = True
        blob = np.zeros(self.SHAPE)
        blob[2:5, 2:6, 1:4] = 1.0
        out = {}
        for index, sid in enumerate(ids):
            case = index % 2 == 0
            r = (0.05 * self.rng.standard_normal(self.SHAPE) + (0.3 * blob if case else 0.0)) * brain
            t = 25.0 * r
            stem = f"sub_{sid}_lesion.nii_conn-{label}"
            compressed = index % 3 != 0
            ext = ".nii.gz" if compressed else ".nii"
            _write_map(os.path.join(folder, f"{stem}_desc-T_funcmap{ext}"), t, self.AFFINE, compressed)
            _write_map(os.path.join(folder, f"{stem}_desc-AvgR_funcmap{ext}"), r, self.AFFINE, compressed)
            if fz:
                _write_map(os.path.join(folder, f"{stem}_desc-AvgR_Fz_funcmap{ext}"), np.arctanh(r), self.AFFINE, compressed)
            out[sid] = t
        return out

    def make_study(self, ids):
        study = Study.create(self.path("study"), name="study")
        volumes = 5.0 + 1.7 * self.rng.permutation(len(ids))
        for index, sid in enumerate(ids):
            study.add_subject(f"sub-{sid}", {"group": "case" if index % 2 == 0 else "control", "volume_ml": float(volumes[index])})
        study.save()
        return study

    def test_import_feeds_cohort_statistics(self):
        ids = [f"M20{index:02d}" for index in range(12)]
        folder = self.path("leaddbs")
        tmaps = self.make_outputs(folder, ids)
        with open(os.path.join(folder, "readme.txt"), "w") as f:
            f.write("not a map")
        study = self.make_study(ids)
        events = []
        record = leaddbs.import_leaddbs_maps(folder, study, emit=lambda event, **fields: events.append((event, fields)))
        out = os.path.join(study.path, "derivatives", "lnm", "conn-GSP1000v2xFullSet")
        self.assertEqual(record["out_dir"], out)
        self.assertEqual((record["n"], record["terms"], record["lesions"]["mapped"], record["unmatched"]), (1000, "GSP-DUT-2014", 12, []))
        rows = cohortmaps.read_maps_tsv(os.path.join(out, "maps.tsv"))
        self.assertEqual([row["participant_id"] for row in rows], sorted(f"sub-{sid}" for sid in ids))
        for row in rows:
            for key in ("AvgR", "AvgFz", "T"):
                self.assertTrue(os.path.isfile(row[key]), row[key])
            self.assertEqual(row["library_version"], "Lead-DBS (imported)")
            self.assertEqual((row["manifest_sha256"], row["subject_ids_sha256"]), ("unknown:GSP1000v2xFullSet", "unknown:GSP1000v2xFullSet"))
        # maps.tsv holds no absolute path; the copied data and header are the original bytes
        with open(os.path.join(out, "maps.tsv"), encoding="utf-8") as f:
            self.assertNotIn(self.tmp, f.read())
        first = f"sub_{ids[0]}_lesion.nii_conn-GSP1000v2xFullSet_desc-T_funcmap.nii"  # an uncompressed original
        with open(os.path.join(folder, first), "rb") as f, gzip.open(os.path.join(out, f"sub-{ids[0]}_conn-GSP1000v2xFullSet_desc-T_funcmap.nii.gz"), "rb") as g:
            self.assertEqual(f.read(), g.read())
        np.testing.assert_array_equal(nifti.read_nifti(rows[1]["T"])[0], tmaps[ids[1]].astype(np.float32))
        sidecar = _load_json(os.path.join(out, f"sub-{ids[0]}_conn-GSP1000v2xFullSet_lnm.json"))
        self.assertEqual(sidecar["provenance"]["source"], "Lead-DBS (imported)")
        self.assertEqual(sidecar["provenance"]["original_files"]["T"]["file"], first)
        self.assertEqual((sidecar["terms"], sidecar["N"], sidecar["compat"]["subject_ids_sha256"]), ("GSP-DUT-2014", 1000, "unknown:GSP1000v2xFullSet"))
        mask, affine, _ = nifti.read_nifti(os.path.join(out, "mask.nii.gz"))
        np.testing.assert_allclose(affine, self.AFFINE)
        self.assertGreater(int(np.count_nonzero(mask)), 50)
        self.assertEqual(_load_json(os.path.join(out, "cohortmaps.json"))["format"], leaddbs.IMPORT_FORMAT)

        # The GLM on the imported T maps (case > control) runs, and the cohort overlap maps too
        design = DesignSpec(contrast="A>B", input="T", inference={"n_perm": 1000, "seed": 3}).save(self.path("design.json"))
        glm_out = self.path("glm")
        code, glm_events = _run_job(glm.main, ["--maps", os.path.join(out, "maps.tsv"), "--participants", os.path.join(study.path, "participants.tsv"), "--design", design,
                                               "--mask", os.path.join(out, "mask.nii.gz"), "--out", glm_out, "--perm", "1000", "--threads", "1"], "glm")
        self.assertEqual(code, 0, glm_events[-3:])
        self.assertTrue(any(name.startswith("group_conn-GSP1000v2xFullSet_desc-caseGtControlTstat") for name in os.listdir(glm_out)))
        cohort_out = self.path("cohort")
        code, cohort_events = _run_job(cohort.main, ["--maps-dir", out, "--mask", os.path.join(out, "mask.nii.gz"), "--out", cohort_out], "cohort")
        self.assertEqual(code, 0, cohort_events[-3:])

        # Re-importing replaces the folder's maps; maps computed here are not overwritten
        again = leaddbs.import_leaddbs_maps(folder, study)
        self.assertEqual(again["lesions"]["mapped"], 12)
        other = self.path("study", "derivatives", "lnm", "conn-Native")
        os.makedirs(other)
        with open(os.path.join(other, "provenance.json"), "w") as f:
            json.dump({"format": export.PROVENANCE_FORMAT}, f)
        relabel = self.path("relabel")
        self.make_outputs(relabel, ids[:2])
        with self.assertRaises(jobs.JobError) as caught:
            leaddbs.import_leaddbs_maps(relabel, study, connectome_label="Native")
        self.assertEqual(caught.exception.code, "exists")

    def test_import_errors_and_cli(self):
        folder = self.path("mixed")
        self.make_outputs(folder, ["A1", "A2"], label="GSP1000v2xFullSet", fz=False)
        self.make_outputs(folder, ["B1"], label="GSP1000v2xFirst100Subjects", fz=False)
        with self.assertRaises(jobs.JobError) as caught:
            leaddbs.import_leaddbs_maps(folder, out_dir=self.path("o1"))
        self.assertEqual(caught.exception.code, "usage")
        record = leaddbs.import_leaddbs_maps(folder, out_dir=self.path("o1"), connectome_label="GSP1000v2xFirst100Subjects")
        self.assertEqual((record["lesions"]["mapped"], record["n"]), (1, 100))
        rows = cohortmaps.read_maps_tsv(record["maps_tsv"])
        self.assertEqual(rows[0]["AvgFz"], "n/a")  # no AvgR_Fz output: never derived from AvgR
        # A map on another grid is refused (the maps are pooled voxel by voxel)
        bad = self.path("bad")
        self.make_outputs(bad, ["C1", "C2"], fz=False)
        _write_map(os.path.join(bad, "sub_C3_lesion.nii_conn-GSP1000v2xFullSet_desc-T_funcmap.nii.gz"), np.zeros((5, 5, 5)), self.AFFINE)
        with self.assertRaises(jobs.JobError) as caught:
            leaddbs.import_leaddbs_maps(bad, out_dir=self.path("o2"))
        self.assertEqual(caught.exception.code, "grid")
        code, events = _run_job(leaddbs.main, ["import", "--folder", folder, "--out", self.path("o3"), "--label", "GSP1000v2xFullSet", "--terms", "none"], "leaddbs")
        self.assertEqual(code, 0, events[-2:])
        self.assertEqual(events[-1]["event"], "done")
        self.assertEqual(events[-1]["n"], 2)
        self.assertIsNone(_load_json(self.path("o3", "cohortmaps.json"))["terms"])

    def test_pooling_guard(self):
        ids = ["P1", "P2", "P3", "P4"]
        first, second = self.path("lead1"), self.path("lead2")
        self.make_outputs(first, ids[:2], fz=False)
        self.make_outputs(second, ids[2:], fz=False)
        self.make_outputs(second, ids[2:], label="GSP1000v2xFirst100Subjects", fz=False)
        a = leaddbs.import_leaddbs_maps(first, out_dir=self.path("a"))
        b = leaddbs.import_leaddbs_maps(second, out_dir=self.path("b"), connectome_label="GSP1000v2xFullSet")
        c = leaddbs.import_leaddbs_maps(second, out_dir=self.path("c"), connectome_label="GSP1000v2xFirst100Subjects")
        mapped = {}
        for record in (a, b):
            mapped.update(glm._map_rows(record["maps_tsv"], "T"))
        design = types.SimpleNamespace(sids=sorted(mapped))
        self.assertEqual(glm._pooling_checks(design, mapped, None), [])  # imported maps of the same label pool
        other = dict(mapped)
        other.update(glm._map_rows(c["maps_tsv"], "T"))
        other["sub-P9"] = (other["sub-P1"][0], None, ("ab" * 32, "cd" * 32, "T", "2", "false", export.LIBRARY_VERSION))  # a map computed here
        for extra in ("sub-P9", *glm._map_rows(c["maps_tsv"], "T")):
            subset = {sid: other[sid] for sid in [*sorted(mapped), extra]}
            checks = glm._pooling_checks(types.SimpleNamespace(sids=sorted(subset)), subset, None)
            self.assertEqual([check.code for check in checks], ["not_poolable"], extra)
        # The records: same label poolable, another label or a native run not
        self.assertEqual(export.check_poolable([a, b]), [])
        self.assertTrue(export.check_poolable([a, c]))
        native = {"bundle": {"manifest_sha256": "ab" * 32}, "subject_ids_sha256": "cd" * 32, "status": "done", "versions": {export.LIBRARY_NAME: export.LIBRARY_VERSION}}
        self.assertTrue(export.check_poolable([a, native]))


class ParityMetricsTest(_TempCase):
    def test_metrics_on_perturbed_pair(self):
        made = connectome.make_synthetic(self.path("b.lnmc"), shape=(14, 16, 14), n_subjects=5, runs=(40,), seed=2)
        bundle = connectome.ConnectomeBundle.open(made["path"])
        weights = made["blobs"]["A"]["mask"].astype(np.float32)[np.newaxis]
        maps = engine.compute_network_maps(bundle, weights)
        ours = maps.t[0].astype(np.float64)
        noise = 0.05 * self.rng.standard_normal(ours.size)
        ref = ours + noise
        ref[:3] = np.nan  # voxels the Lead-DBS map does not cover are left out
        metrics = leaddbs.parity_metrics(ours, ref, maps.avg_r[0], maps.avg_r[0] * 1.0, covariates=None)
        valid = np.isfinite(ref)
        self.assertEqual(metrics["n_voxels"], int(valid.sum()))
        self.assertAlmostEqual(metrics["r_T"], float(np.corrcoef(ours[valid], ref[valid])[0, 1]), places=10)
        self.assertGreater(metrics["r_T"], 0.99)
        self.assertAlmostEqual(metrics["r_AvgR"], 1.0, places=10)
        self.assertIsNone(metrics["partial_r_T"])
        self.assertAlmostEqual(metrics["max_abs_dT"], float(np.abs(noise[valid]).max()), places=10)

        def status(values):
            return np.where(values >= 7, 1, np.where(values <= -7, -1, 0))

        self.assertEqual(metrics["n_flip"], int(np.count_nonzero(status(ours[valid]) != status(ref[valid]))))
        self.assertEqual(metrics["n_supra_ours"], int(np.count_nonzero(status(ours[valid]))))
        k = int(np.ceil(0.1 * valid.sum()))
        idx = np.flatnonzero(valid)
        top_a = set(idx[np.argsort(-ours[idx], kind="stable")[:k]])
        top_b = set(idx[np.argsort(-ref[idx], kind="stable")[:k]])
        self.assertAlmostEqual(metrics["dice_top"], len(top_a & top_b) / k, places=10)
        self.assertGreater(metrics["dice_top"], 0.8)
        # A partial correlation given covariate maps, and the exact-copy case
        covariates = np.vstack([np.linspace(0, 1, ours.size), self.rng.standard_normal(ours.size)])
        self.assertTrue(np.isfinite(leaddbs.parity_metrics(ours, ref, covariates=covariates)["partial_r_T"]))
        same = leaddbs.parity_metrics(ours, ours.copy())
        self.assertEqual((same["n_flip"], same["max_abs_dT"], same["dice_top"]), (0, 0.0, 1.0))
        # Flips: a voxel crossing 7 and a sign change at the threshold both count
        a = np.array([8.0, 6.9, -7.5, 7.2, 0.0])
        b = np.array([6.0, 7.1, 7.5, 7.3, 0.0])
        self.assertEqual(leaddbs.threshold_flips(a, b)["n_flip"], 3)


class ParityRunTest(_TempCase):
    """run_parity end to end with a local catalog whose reference files are adopted into a temporary cache."""

    def setUp(self):
        super().setUp()
        made = connectome.make_synthetic(self.path("b.lnmc"), shape=(14, 16, 14), n_subjects=6, runs=(40,), seed=5, name="Synthetic Parity")
        self.bundle = connectome.ConnectomeBundle.open(made["path"])
        self.cache = self.path("cache")
        src = self.path("src")
        os.makedirs(src)
        entries, lesions = [], []
        blobs = {"lesA": made["blobs"]["A"]["mask"], "lesB": made["blobs"]["B"]["mask"]}
        maps = engine.compute_network_maps(self.bundle, np.vstack([blobs["lesA"], blobs["lesB"]]).astype(np.float32))
        self.expected = {}
        for index, (lesion_id, mask_vec) in enumerate(blobs.items()):
            mask_path = _save(os.path.join(src, f"{lesion_id}_mask.nii.gz"), engine.unmask(mask_vec.astype(np.float32), self.bundle), self.bundle.affine)
            t = maps.t[index] + 0.1 * self.rng.standard_normal(maps.t.shape[1])  # "Lead-DBS": our map plus a small difference
            t_path = _save(os.path.join(src, f"{lesion_id}_T.nii.gz"), engine.unmask(t, self.bundle), self.bundle.affine)
            r_path = _save(os.path.join(src, f"{lesion_id}_AvgR.nii.gz"), engine.unmask(maps.avg_r[index], self.bundle), self.bundle.affine)
            self.expected[lesion_id] = t
            row = {"id": lesion_id, "set": "SYNTHETIC"}
            for key, path in (("mask", mask_path), ("AvgR", r_path), ("T", t_path)):
                entries.append(self.entry(f"{lesion_id}/{key}", path))
                row[key] = f"{lesion_id}/{key}"
            lesions.append(row)
        connectome.compute_degree(self.bundle, n_seeds=50)
        degree_path = _save(os.path.join(src, "degree.nii.gz"), engine.unmask(self.bundle.degree, self.bundle), self.bundle.affine)
        entries.append(self.entry("degree", degree_path))
        shipped = leaddbs.load_parity_catalog().data
        self.catalog_path = self.path("parity.json")
        with open(self.catalog_path, "w", encoding="utf-8") as f:
            json.dump({**{key: shipped[key] for key in ("format", "version", "source", "reference", "thresholds")}, "catalog": "parity_test", "lesions": lesions, "entries": entries}, f)
        self.sources = {entry["id"]: entry["source"] for entry in entries}

    def entry(self, entry_id, path):
        return {"id": entry_id, "title": entry_id, "kind": "leaddbs_map", "urls": [f"https://example.invalid/{entry_id}"], "filename": os.path.basename(path),
                "checksum": "record-on-first-download", "git_blob": leaddbs.git_blob_sha1(path), "licence": "GSP-DUT-2014", "badge": "amber", "citation": "test",
                "source": path}

    def adopt_all(self):
        catalog = leaddbs.load_parity_catalog(self.catalog_path)
        for resource in catalog:
            fetch.adopt(resource, self.sources[resource.id], self.cache)
        return catalog

    def test_terms_and_offline_guards(self):
        catalog = leaddbs.load_parity_catalog(self.catalog_path)
        with self.assertRaises(jobs.JobError) as caught:
            leaddbs.run_parity(self.bundle, cache=self.cache, out=self.path("out"), catalog=catalog)
        self.assertEqual(caught.exception.code, "terms")
        self.assertFalse(os.path.exists(self.cache))  # nothing fetched before the terms
        self.assertEqual(len(leaddbs.missing_files(catalog, cache=self.cache)), 7)
        with self.assertRaises(jobs.JobError) as caught:
            leaddbs.run_parity(self.bundle, cache=self.cache, out=self.path("out"), catalog=catalog, terms_accepted=True, register=False)
        self.assertEqual(caught.exception.code, "fetch_offline")
        code, events = _run_job(leaddbs.main, ["parity", "--bundle", self.bundle.path, "--out", self.path("cli")], "leaddbs")
        self.assertEqual(code, jobs.EXIT_USAGE)
        self.assertEqual(events[-1]["code"], "terms")

    def test_parity_run(self):
        catalog = self.adopt_all()
        self.assertEqual(leaddbs.missing_files(catalog, cache=self.cache), [])
        out = self.path("out")
        result = leaddbs.run_parity(self.bundle, cache=self.cache, out=out, catalog=catalog, terms_accepted=True, save_maps=True)
        data = _load_json(os.path.join(out, "parity.json"))
        self.assertEqual(data["format"], leaddbs.PARITY_FORMAT)
        self.assertEqual([row["id"] for row in data["lesions"]], ["lesA", "lesB"])
        for row in data["lesions"]:
            metrics = row["metrics"]
            self.assertGreater(metrics["r_T"], 0.95)
            self.assertAlmostEqual(metrics["r_AvgR"], 1.0, places=5)
            self.assertLess(metrics["max_abs_dT"], 1.0)
            self.assertIsNone(metrics["partial_r_T"])
        self.assertTrue(data["release_differences_included"])  # a synthetic bundle is not the Lead-DBS release
        self.assertIn(leaddbs.RELEASE_CAVEAT, data["caveats"])
        self.assertTrue(any("N=6" in text for text in data["caveats"]))
        self.assertAlmostEqual(data["degree"]["r"], 1.0, places=4)
        self.assertEqual(data["terms"], "GSP-DUT-2014")
        self.assertIn("no agreement level was set in advance", data["methods"])
        self.assertIn("not computed", data["methods"])  # no reference maps: the partial r is reported as such
        self.assertNotIn(self.tmp, json.dumps(data))  # no local path in the shareable report
        self.assertEqual(sorted(data["maps"]), sorted(f"maps/{name}_conn-SyntheticParityxFullSet_desc-{desc}_funcmap.nii.gz" for name in ("lesA", "lesB") for desc in ("AvgR", "AvgRFz", "T")))
        with open(os.path.join(out, "methods.md"), encoding="utf-8") as f:
            self.assertIn(data["methods"], f.read())
        registry = runs.load(out)
        self.assertEqual([(record["kind"], record["status"]) for record in registry.values()], [("parity", "done")])
        self.assertEqual(result["summary"]["n_lesions"], 2)

        # With the connectome's reference maps the partial r given [muC_r, PC1] is reported; the command line gives the same numbers
        with contextlib.redirect_stdout(io.StringIO()):  # the build emits job events
            reference.build(self.bundle, n_seeds=40, k=2)
        out2 = self.path("out2")
        code, events = _run_job(leaddbs.main, ["parity", "--bundle", self.bundle.path, "--out", out2, "--accept-terms", "GSP-DUT-2014", "--cache", self.cache,
                                               "--offline", "--catalog", self.catalog_path, "--threads", "1"], "leaddbs")
        self.assertEqual(code, 0, events[-3:])
        self.assertEqual(sum(1 for event in events if event.get("kind") == "parity_lesion"), 2)
        data2 = _load_json(os.path.join(out2, "parity.json"))
        for row, row2 in zip(data["lesions"], data2["lesions"], strict=True):
            self.assertAlmostEqual(row["metrics"]["r_T"], row2["metrics"]["r_T"], places=6)
            self.assertTrue(np.isfinite(row2["metrics"]["partial_r_T"]))
        self.assertEqual(data2["partial_covariates"], ["mu_r", "pc1"])

    def test_pinned_blob_mismatch_and_cli(self):
        catalog = self.adopt_all()
        resource = catalog.get("lesA/T")
        cached = fetch.cached_path(resource, self.cache)
        # The cache record was made from this file: replace the pinned blob id instead of the bytes
        with open(self.catalog_path, encoding="utf-8") as f:
            data = json.load(f)
        for entry in data["entries"]:
            if entry["id"] == "lesA/T":
                entry["git_blob"] = "0" * 40
        tampered = self.path("tampered.json")
        with open(tampered, "w", encoding="utf-8") as f:
            json.dump(data, f)
        with self.assertRaises(jobs.JobError) as caught:
            leaddbs.fetch_reference(leaddbs.load_parity_catalog(tampered), leaddbs.parity_lesions(leaddbs.load_parity_catalog(tampered), ["lesA"]), cache=self.cache)
        self.assertEqual(caught.exception.code, "checksum")
        self.assertTrue(os.path.isfile(cached))
        self.assertEqual(sorted(leaddbs.fetch_reference(catalog, leaddbs.parity_lesions(catalog, ["lesB"]), cache=self.cache)), ["degree", "lesB/AvgR", "lesB/T", "lesB/mask"])
        with self.assertRaises(jobs.JobError):
            leaddbs.parity_lesions(catalog, ["nope"])


class CatalogTest(unittest.TestCase):
    def test_shipped_catalog(self):
        catalog = leaddbs.load_parity_catalog()
        data = catalog.data
        self.assertEqual((data["format"], data["version"]), (fetch.CATALOG_FORMAT, fetch.CATALOG_VERSION))
        commit = data["source"]["commit"]
        self.assertTrue(commit.startswith("ca410e6"))
        self.assertEqual(data["source"]["licence"], "none stated (vdH repo)")
        lesions = data["lesions"]
        self.assertEqual(len(lesions), 24)
        self.assertEqual({row["set"] for row in lesions}, {"APHASIA_RECOVERY", "ADDICTION", "EPILEPSY", "SyntheticLesions"})
        self.assertEqual(len({row["id"] for row in lesions}), 24)
        for row in lesions:
            for key in ("mask", "AvgR", "T"):
                resource = catalog.get(row[key])
                self.assertEqual(len(resource.urls), 1)
                self.assertTrue(resource.urls[0].startswith(f"https://raw.githubusercontent.com/dutchconnectomelab/lesionnetworkmapping/{commit}/"), resource.urls[0])
                self.assertEqual(resource.get("checksum"), fetch.RECORD_ON_FIRST_DOWNLOAD)
                self.assertRegex(resource.get("git_blob"), r"^[0-9a-f]{40}$")
                if key == "mask":
                    self.assertEqual((resource.licence, resource.badge), ("not-stated", "grey"))
                else:
                    self.assertIn("GSP1000v2xFullSet_desc-" + key, resource.urls[0])
                    self.assertEqual((resource.licence, resource.get("terms"), resource.badge), ("GSP-DUT-2014", "GSP-DUT-2014", "amber"))
        self.assertEqual(catalog.get("degree").get("terms"), "GSP-DUT-2014")
        # Nothing GSP-derived is bundled with the module
        lib = os.path.join(MODULE_DIR, "LesionNetworkMappingLib")
        for root, _, files in os.walk(MODULE_DIR):
            for name in files:
                self.assertFalse(name.endswith((".nii", ".nii.gz", ".npy", ".mat")) and "Testing" not in root, os.path.join(root, name))
        self.assertTrue(os.path.isfile(os.path.join(lib, "catalog", "parity_leaddbs.json")))


if __name__ == "__main__":
    unittest.main()

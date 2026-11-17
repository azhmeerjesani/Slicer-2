"""The publication bundle holds the maps and seeds of a seed-set (coordinates) run (BB-1).

The Map widget maps Seed = Coordinates in a study with ``cohortmaps --lesions-tsv ... --run-root <study>``, without
``--study``: the run keeps its per-seed maps in its own cache folder (``runs/<stamp>_coordinates/cache/...``). The
publication manifest skipped every cache folder, so "Export publication bundle..." left out all files of that run
(maps, foci, seeds) while its methods.md and checklist.md described it; with another run's map in the study, there was
not even a warning. The manifest now lists a seed-set run's seeds.json, foci.tsv, lesions.tsv, maps.tsv and seed images,
and the maps its maps.tsv lists below the study, so the bundle's maps.tsv resolves to the copied maps.

The bundle test needs the Slicer application (the Report card module imports qt)::

    ./Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<module>/Testing/Python'], 'LesionNetworkMappingBundleSeedRunTest'); slicer.app.exit(0)"
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

try:
    import slicer

    _HAS_APP = getattr(slicer, "app", None) is not None
except ImportError:
    _HAS_APP = False

STAMP = "runs/20261113-000000_coordinates"
CACHE = "cache/conn-Syn_subj-0123abcd"


class LesionNetworkMappingBundleSeedRunTest(unittest.TestCase):
    def setUp(self):
        from LesionNetworkMappingLib import cohortmaps
        from LesionNetworkMappingLib.nifti import write_nifti

        self.work = os.path.realpath(tempfile.mkdtemp(prefix="lnm-bundle-seedrun-"))
        self.study = os.path.join(self.work, "Study")
        lnm = os.path.join(self.study, "derivatives", "lnm")
        self.run = os.path.join(lnm, *STAMP.split("/"))
        os.makedirs(os.path.join(self.run, "seeds"))
        affine = np.eye(4)
        volume = np.zeros((4, 4, 4), dtype=np.float32)
        volume[1:3, 1:3, 1:3] = 1.0
        rows, lesions = [], ["participant_id\tlesion\tlesion_sha256"]
        for sid in ("S1", "S2"):
            write_nifti(os.path.join(self.run, "seeds", f"{sid}_seed.nii.gz"), volume, affine, dtype=np.float32)
            lesions.append(f"{sid}\tseeds/{sid}_seed.nii.gz\t{sid.lower() * 32}")
            key = (sid.lower() * 64)[:64]
            entry = os.path.join(self.run, *CACHE.split("/"), key)
            os.makedirs(entry)
            row = {"participant_id": sid, "lesion": os.path.join(self.run, "seeds", f"{sid}_seed.nii.gz"), "key": key}
            for name in cohortmaps.MAP_KEYS:
                path = os.path.join(entry, f"lesion-{key[:12]}_conn-Syn_desc-{name}_funcmap.nii.gz")
                write_nifti(path, volume, affine, dtype=np.float32)
                row[name] = path
            with open(os.path.join(entry, "entry.json"), "w", encoding="utf-8") as f:
                json.dump({"bundle": "Syn"}, f)
            rows.append(row)
        cohortmaps.write_maps_tsv(os.path.join(self.run, "maps.tsv"), rows)
        with open(os.path.join(self.run, "lesions.tsv"), "w", encoding="utf-8") as f:
            f.write("\n".join(lesions) + "\n")
        with open(os.path.join(self.run, "foci.tsv"), "w", encoding="utf-8") as f:
            f.write("study\tx\ty\tz\tspace\nS1\t10\t-20\t30\tMNI\nS2\t-12\t8\t40\tMNI\n")
        with open(os.path.join(self.run, "seeds.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-lnm-seeds", "version": 1, "kind": "coordinates", "seeds": []}, f)
        # Another run's unthresholded map: the "unthresholded map" kind is present, so a missing-kind warning cannot stand in
        other = os.path.join(lnm, "conn-Syn", "sub-01")
        os.makedirs(other)
        write_nifti(os.path.join(other, "sub-01_conn-Syn_desc-T_funcmap.nii.gz"), volume, affine, dtype=np.float32)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def expected(self):
        from LesionNetworkMappingLib import cohortmaps

        maps = [os.path.relpath(row[name], self.study).replace(os.sep, "/")
                for row in cohortmaps.read_maps_tsv(os.path.join(self.run, "maps.tsv")) for name in cohortmaps.MAP_KEYS]
        self.assertEqual(len(maps), 6)
        seeds = [f"derivatives/lnm/{STAMP}/{name}" for name in ("seeds.json", "foci.tsv", "lesions.tsv", "maps.tsv", "seeds/S1_seed.nii.gz", "seeds/S2_seed.nii.gz")]
        return maps, seeds

    def test_manifest_lists_the_seed_run(self):
        from LesionNetworkMappingLib import export

        manifest = export.publication_manifest(self.study, hashes=False)
        kinds = {entry["path"]: entry["kind"] for entry in manifest["files"]}
        maps, seeds = self.expected()
        for path in maps:
            self.assertEqual(kinds.get(path), "unthresholded_map", path)
        for path in seeds:
            self.assertEqual(kinds.get(path), "seeds", path)
        self.assertFalse([path for path in kinds if path.endswith(("entry.json", "weights.npz"))])  # only the maps of the cache
        self.assertEqual(len(kinds), len(manifest["files"]))  # each file once

    @unittest.skipUnless(_HAS_APP, "needs the Slicer application")
    def test_bundle_holds_the_seed_run_maps(self):
        from LesionNetworkMappingLib import cohortmaps
        from LesionNetworkMappingUI import card_report

        result = card_report.exportBundle(self.study)
        files = set(result["files"])
        maps, seeds = self.expected()
        for path in maps + seeds:
            self.assertIn(path[len("derivatives/lnm/") :], files)
        # The bundle's maps.tsv resolves to the copied maps (its paths are relative, no absolute local path)
        copied = os.path.join(result["dir"], *STAMP.split("/"), "maps.tsv")
        with open(copied, encoding="utf-8") as f:
            self.assertNotIn(self.work, f.read())
        for row in cohortmaps.read_maps_tsv(copied):
            for name in cohortmaps.MAP_KEYS:
                self.assertTrue(os.path.isfile(row[name]), row[name])
                self.assertTrue(os.path.realpath(row[name]).startswith(os.path.realpath(result["dir"]) + os.sep), row[name])
        with open(os.path.join(result["dir"], "manifest.json"), encoding="utf-8") as f:
            self.assertNotIn(self.work, f.read())


if __name__ == "__main__":
    unittest.main()

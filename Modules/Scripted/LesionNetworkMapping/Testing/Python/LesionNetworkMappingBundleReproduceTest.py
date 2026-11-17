"""The publication bundle's reproduce scripts run from the bundle (BB-1).

reproduce_pipeline.sh runs ``pipeline run --config "$HERE/pipeline.json"``, but the Report card's "Export publication
bundle..." copied the script without its pipeline.json (and copied it to the top-level reproduce.sh), so both scripts of
the bundle failed with "No such file or directory: .../publication/<UTC>/pipeline.json". The bundle now holds a
pipeline.json next to each conn-*/reproduce_pipeline.sh, its study and connectome paths rebased onto the bundle folder,
and the top-level reproduce.sh runs that script from its own folder.

These tests need the Slicer application (the Report card module imports qt)::

    ./Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<module>/Testing/Python'], 'LesionNetworkMappingBundleReproduceTest'); slicer.app.exit(0)"
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

try:
    import slicer

    _HAS_APP = getattr(slicer, "app", None) is not None
except ImportError:
    _HAS_APP = False

# Stands in for pipeline._write_reproduce_pipeline's script: it reads "$HERE/pipeline.json" the same way
STUB = '#!/bin/sh\nset -e\nHERE="$(cd "$(dirname "$0")" && pwd)"\ncat "$HERE/pipeline.json"\n'


@unittest.skipUnless(_HAS_APP and shutil.which("sh"), "needs the Slicer application and sh")
class LesionNetworkMappingBundleReproduceTest(unittest.TestCase):
    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp(prefix="lnm-bundle-reproduce-"))
        self.study = os.path.join(self.work, "Study with spaces")
        self.connectome = os.path.join(self.work, "Synthetic connectome.lnmc")
        os.makedirs(self.connectome)
        self.conn = os.path.join(self.study, "derivatives", "lnm", "conn-Synthetic")
        os.makedirs(self.conn)
        with open(os.path.join(self.conn, "reproduce_pipeline.sh"), "w", encoding="utf-8", newline="\n") as f:
            f.write(STUB)
        os.chmod(os.path.join(self.conn, "reproduce_pipeline.sh"), 0o755)

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def writeConfig(self):
        # Paths relative to the output folder, as pipeline.PipelineConfig.to_json(base=out) stores them
        config = {"study": "../../..", "bundle": "../../../../Synthetic connectome.lnmc", "perm": 1000}
        with open(os.path.join(self.conn, "pipeline.json"), "w", encoding="utf-8") as f:
            json.dump({"format": "slicer-lnm-pipeline", "version": 1, "config": config, "status": "done"}, f)

    def export(self):
        from LesionNetworkMappingUI import card_report

        return card_report.exportBundle(self.study)

    def run_script(self, path):
        return subprocess.run(["sh", path], cwd=self.work, capture_output=True, text=True, timeout=60, check=False)

    def test_bundle_scripts_find_the_rebased_pipeline_json(self):
        self.writeConfig()
        result = self.export()
        out = result["dir"]
        self.assertIn("conn-Synthetic/pipeline.json", result["files"])
        for script in ("reproduce.sh", "conn-Synthetic/reproduce_pipeline.sh"):
            done = self.run_script(os.path.join(out, *script.split("/")))
            self.assertEqual(done.returncode, 0, f"{script}: {done.stderr}")
            config = json.loads(done.stdout)["config"]
            base = os.path.join(out, "conn-Synthetic")
            for key, expected in (("study", self.study), ("bundle", self.connectome)):
                self.assertFalse(os.path.isabs(config[key]), config[key])  # no absolute local path in the bundle
                self.assertEqual(os.path.realpath(os.path.join(base, config[key])), expected)
            self.assertEqual(config["perm"], 1000)
        # The study's own pipeline.json is unchanged
        with open(os.path.join(self.conn, "pipeline.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["config"]["study"], "../../..")
        with open(os.path.join(out, "manifest.json"), encoding="utf-8") as f:
            kinds = {entry["path"]: entry["kind"] for entry in json.load(f)["files"]}
        self.assertEqual(kinds.get("conn-Synthetic/pipeline.json"), "reproduce")
        self.assertEqual(kinds.get("reproduce.sh"), "reproduce")

    def test_pipeline_script_without_configuration_is_left_out(self):
        result = self.export()
        out = result["dir"]
        self.assertFalse(os.path.exists(os.path.join(out, "conn-Synthetic", "reproduce_pipeline.sh")))
        self.assertFalse(os.path.exists(os.path.join(out, "reproduce.sh")))
        self.assertTrue(any("reproduce_pipeline.sh was left out" in warning for warning in result["warnings"]), result["warnings"])
        self.assertFalse(any(self.study in warning for warning in result["warnings"]), result["warnings"])


if __name__ == "__main__":
    unittest.main()

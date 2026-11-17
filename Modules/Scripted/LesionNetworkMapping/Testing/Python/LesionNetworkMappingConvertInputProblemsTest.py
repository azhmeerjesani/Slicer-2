"""Convert... with an unsuitable folder says what is wrong in plain words (bug convert-wrong-input-raw-exception-messages).

A NIfTI folder holding only a 3D image used to pass the size estimate ("1 runs (1 time points)") and fail later with
"ValueError: Expected a 4D time series ..."; a NIfTI folder chosen as a Lead-DBS connectome showed
"FileNotFoundError: [Errno 2] ...". The estimate now refuses both with a sentence that names the problem. No
application is needed::

    python3 Utilities/Scripts/StudioTestRunner.py --home $(mktemp -d) unittest LesionNetworkMapping LesionNetworkMappingConvertInputProblemsTest
"""

import os
import shutil
import tempfile
import unittest

import numpy as np

from LesionNetworkMappingLib import manager, nifti

AFFINE = np.diag([2.0, 2.0, 2.0, 1.0])


class LesionNetworkMappingConvertInputProblemsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnm convert input ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mask = os.path.join(self.tmp, "mask.nii.gz")
        nifti.write_nifti(self.mask, np.ones((4, 4, 4), np.uint8), AFFINE)
        self.runs = os.path.join(self.tmp, "runs")
        os.makedirs(self.runs)

    def test_a_3d_only_folder_is_refused_by_the_estimate(self):
        nifti.write_nifti(os.path.join(self.runs, "sub-01_t1.nii.gz"), np.zeros((4, 4, 4), np.float32), AFFINE)
        with self.assertRaises(ValueError) as caught:
            manager.estimate_conversion("nifti", self.runs, mask=self.mask)
        text = manager.input_problem(caught.exception)
        self.assertIn("no 4D time series", text)
        self.assertIn("sub-01_t1.nii.gz is a 3D image", text)
        self.assertNotIn("ValueError", text)

    def test_a_4d_folder_still_estimates(self):
        nifti.write_nifti(os.path.join(self.runs, "sub-01_bold.nii.gz"), np.zeros((4, 4, 4, 5), np.float32), AFFINE)
        estimate = manager.estimate_conversion("nifti", self.runs, mask=self.mask)
        self.assertEqual((estimate["runs"], estimate["time_points"]), (1, 5))

    def test_a_nifti_folder_is_not_a_leaddbs_folder(self):
        nifti.write_nifti(os.path.join(self.runs, "sub-01_bold.nii.gz"), np.zeros((4, 4, 4, 5), np.float32), AFFINE)
        with self.assertRaises(Exception) as caught:
            manager.estimate_conversion("leaddbs", self.runs)
        text = manager.input_problem(caught.exception)
        self.assertIn("Not a Lead-DBS connectome folder: dataset_volsurf.mat is missing", text)
        self.assertNotIn("Errno", text)
        self.assertNotIn("FileNotFoundError", text)

    def test_input_problem_wording(self):
        self.assertEqual(manager.input_problem(ValueError("plain")), "plain")
        self.assertEqual(manager.input_problem(FileNotFoundError(2, "No such file or directory", "/x/y/a.mat")), "File not found: a.mat")
        self.assertEqual(manager.input_problem(RuntimeError("boom")), "RuntimeError: boom")


if __name__ == "__main__":
    unittest.main()

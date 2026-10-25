"""Unit tests of the peak region names of Map's Result card (atlases.peak_regions / region_text / region_atlases).

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingPeakRegionsTest.py -v
"""

import os
import sys
import tempfile
import unittest

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

import numpy as np

from LesionNetworkMappingLib import atlases, fetch

SHAPE = (20, 22, 18)
# 2 mm voxels; x = 2 i - 19 is never 0, so every voxel is left (i <= 9) or right (i >= 10)
AFFINE = np.array([[2.0, 0, 0, -19.0], [0, 2.0, 0, -21.0], [0, 0, 2.0, -17.0], [0, 0, 0, 1.0]])


def world(ijk):
    return (AFFINE @ np.array([*ijk, 1.0]))[:3]


def subcortical():
    """Left and right "Cerebral White Matter" halves (generic) with a "Left Caudate" box on the left."""
    data = np.zeros(SHAPE, np.int16)
    data[:10] = 1
    data[10:] = 2
    data[3:6, 3:6, 3:6] = 3
    return atlases.Atlas("subcortical", data, AFFINE, {1: "Left Cerebral White Matter", 2: "Right Cerebral White Matter", 3: "Left Caudate"},
                         family="fsl6", space="MNI152NLin6Asym")


def cortical():
    """"Angular Gyrus" over the whole upper half (no side in the name)."""
    data = np.zeros(SHAPE, np.int16)
    data[:, 11:, :] = 1
    return atlases.Atlas("cortical", data, AFFINE, {1: "Angular Gyrus"}, family="fsl6", space="MNI152NLin6Asym")


class PeakRegionsTest(unittest.TestCase):

    def test_region_text(self):
        self.assertEqual(atlases.region_text("Left Caudate"), "L caudate")
        self.assertEqual(atlases.region_text("Right Thalamus"), "R thalamus")
        self.assertEqual(atlases.region_text("Angular Gyrus", -40.0), "L angular gyrus")
        self.assertEqual(atlases.region_text("Angular Gyrus", 40.0), "R angular gyrus")
        # On the midline the side is not stated
        self.assertEqual(atlases.region_text("Brain-Stem", 0.4), "Brain-Stem")
        # Midline structures take no side from x
        self.assertEqual(atlases.region_text("Brain-Stem", 6.0), "Brain-Stem")
        self.assertEqual(atlases.region_text("7Networks_LH_Vis_1"), "L Vis 1")
        self.assertEqual(atlases.region_text("Precentral_R"), "R precentral")
        self.assertEqual(atlases.region_text("THA-lh"), "L THA")
        # Numerals and acronyms keep their case
        self.assertEqual(atlases.region_text("Cerebellum Crus I", 30.0), "R cerebellum crus I")

    def test_first_specific_label_wins(self):
        points = [world((4, 4, 4)), world((15, 15, 9)), world((15, 4, 9))]
        names = atlases.peak_regions(points, atlases=[subcortical(), cortical()])
        # In the caudate box: the subcortical name. In white matter (broad): the cortical atlas names it, with the side
        # from x. In white matter and outside the cortical label: the broad label (the peak is inside the atlas)
        self.assertEqual(names, ["L caudate", "R angular gyrus", "R cerebral white matter"])

    def test_broad_labels_are_kept(self):
        # With the subcortical atlas alone, a white matter peak shows its broad label and a brainstem peak its own
        data = subcortical().data.copy()
        data[8:12, 15:18, 3:6] = 4
        atlas = atlases.Atlas("subcortical", data, AFFINE, {1: "Left Cerebral White Matter", 2: "Right Cerebral White Matter",
                                                            3: "Left Caudate", 4: "Brain-Stem"}, family="fsl6")
        names = atlases.peak_regions([world((15, 15, 9)), world((10, 16, 4)), world((4, 4, 4))], atlases=[atlas])
        self.assertEqual(names, ["R cerebral white matter", "Brain-Stem", "L caudate"])
        # Outside atlas only where no atlas has a label
        empty = atlases.Atlas("empty", np.zeros(SHAPE, np.int16), AFFINE, {}, family="fsl6")
        self.assertEqual(atlases.peak_regions([world((15, 15, 9))], atlases=[empty]), [atlases.OUTSIDE_ATLAS])

    def test_later_atlases_are_not_read_when_every_peak_is_named(self):
        loaded = []
        original = atlases._region_atlas

        def regionAtlas(resource, folder):
            loaded.append(resource)
            return [subcortical(), cortical()][len(loaded) - 1]

        resources = [fetch.Resource.coerce({"id": f"a{index}", "title": "A", "url": "https://example.org/a.nii.gz"}) for index in range(2)]
        originalResources = atlases._region_resources
        atlases._region_atlas = regionAtlas
        atlases._region_resources = lambda folder, family=None: resources
        try:
            self.assertEqual(atlases.peak_regions([world((4, 4, 4))]), ["L caudate"])
        finally:
            atlases._region_atlas = original
            atlases._region_resources = originalResources
        self.assertEqual(len(loaded), 1)

    def test_outside_the_atlas_grid(self):
        names = atlases.peak_regions([[500.0, 0.0, 0.0]], atlases=[subcortical()])
        self.assertEqual(names, [atlases.OUTSIDE_ATLAS])

    def test_space_family(self):
        other = atlases.Atlas("other", subcortical().data, AFFINE, {3: "Left Putamen"}, family="2009")
        point = [world((4, 4, 4))]
        # Atlases of another template family are not used
        self.assertIsNone(atlases.peak_regions(point, space="MNI152NLin6Asym", atlases=[other]))
        self.assertEqual(atlases.peak_regions(point, space="MNI152NLin2009cAsym", atlases=[other]), ["L putamen"])
        self.assertEqual(atlases.peak_regions(point, space="MNI152NLin6Asym", atlases=[other, subcortical()]), ["L caudate"])

    def test_no_atlas_cached_downloads_nothing(self):
        # An empty cache folder: no atlas, None ("nothing can be said"), and no file is written (nothing is downloaded)
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(atlases.region_atlases(folder), [])
            self.assertIsNone(atlases.peak_regions([[0.0, 0.0, 0.0]], cache=folder))
            self.assertEqual(os.listdir(folder), [])

    def test_empty_points(self):
        self.assertEqual(atlases.peak_regions(np.zeros((0, 3)), atlases=[subcortical()]), [])


if __name__ == "__main__":
    unittest.main()

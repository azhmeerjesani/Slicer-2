import unittest

import vtk

import slicer


class SegmentationsClosedSurfaceTest(unittest.TestCase):
    """Closed surface conversion of labelmaps that have no foreground voxels and are thinner than 3 voxels.

    Such a labelmap is what a segment keeps when it was reduced to a tiny remnant and then overwritten by another
    segment. vtkDiscreteFlyingEdges3D fails (and logs an error) on an image dimension of 1, so the conversion must
    produce an empty surface without any error.
    """

    def setUp(self):
        slicer.mrmlScene.Clear(0)

    def tearDown(self):
        slicer.mrmlScene.Clear(0)

    def _convertLabelmapOfExtent(self, extent, fillValue):
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
        segmentationNode.CreateDefaultDisplayNodes()
        segmentation = segmentationNode.GetSegmentation()
        segmentation.SetMasterRepresentationName("Binary labelmap")
        segmentId = segmentation.AddEmptySegment("S")
        labelmap = slicer.vtkOrientedImageData()
        labelmap.SetExtent(extent)
        labelmap.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
        labelmap.GetPointData().GetScalars().Fill(fillValue)
        slicer.vtkSlicerSegmentationsModuleLogic.SetBinaryLabelmapToSegment(
            labelmap,
            segmentationNode,
            segmentId,
            slicer.vtkSlicerSegmentationsModuleLogic.MODE_REPLACE,
            labelmap.GetExtent(),
        )

        # Capture everything that VTK reports while converting
        previousWindow = vtk.vtkOutputWindow.GetInstance()
        capture = vtk.vtkStringOutputWindow()
        vtk.vtkOutputWindow.SetInstance(capture)
        try:
            created = segmentationNode.CreateClosedSurfaceRepresentation()
        finally:
            vtk.vtkOutputWindow.SetInstance(previousWindow)
        polyData = segmentation.GetSegment(segmentId).GetRepresentation("Closed surface")
        return created, polyData, capture.GetOutput()

    def test_EmptyOneVoxelLabelmapConvertsWithoutError(self):
        created, polyData, messages = self._convertLabelmapOfExtent([25, 25, 25, 25, 14, 14], 0)
        self.assertEqual(messages, "")
        self.assertTrue(created)
        self.assertEqual(polyData.GetNumberOfPoints() if polyData else 0, 0)

    def test_EmptyThinLabelmapsConvertWithoutError(self):
        for extent in ([0, 0, 0, 5, 0, 5], [0, 5, 0, 0, 0, 5], [0, 5, 0, 5, 0, 1], [0, 1, 0, 1, 0, 1]):
            created, polyData, messages = self._convertLabelmapOfExtent(extent, 0)
            self.assertEqual(messages, "", extent)
            self.assertTrue(created, extent)
            self.assertEqual(polyData.GetNumberOfPoints() if polyData else 0, 0, extent)
            slicer.mrmlScene.Clear(0)

    def test_OneVoxelLabelmapWithForegroundStillHasSurface(self):
        created, polyData, messages = self._convertLabelmapOfExtent([25, 25, 25, 25, 14, 14], 1)
        self.assertEqual(messages, "")
        self.assertTrue(created)
        self.assertGreater(polyData.GetNumberOfPoints(), 0)

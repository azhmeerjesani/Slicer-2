"""Tests of the Scissors Segment Editor effect with a limited slice cut (Symmetric mode).

The cut is drawn with mouse events in the Red slice view, so the test needs the main window.
Run with: Slicer --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'SegmentEditorScissorsSliceCutTest')"
"""

import unittest

import numpy as np
import vtk

import slicer

# Grid (K, J, I) and a box segment that the drawn rectangle encloses completely
SHAPE = (30, 64, 64)
BOX = (slice(2, 28), slice(25, 39), slice(25, 39))
BOX_SLICE_VOXELS = 14 * 14
CUT_SLICE = 14
RECTANGLE_CORNERS_IJ = ((15, 15), (48, 48))


class SegmentEditorScissorsSliceCutTest(unittest.TestCase):
    """Scissors with Slice cut = Symmetric must only change the slices of the slab, whatever the thickness."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.editor = None

    def tearDown(self):
        if self.editor is not None:
            self.editor.setActiveEffectByName("")
            self.editor.setMRMLScene(None)
            self.editor = None
        slicer.mrmlScene.Clear(0)

    #
    # Helpers
    #

    def createScene(self, sliceSpacing):
        image = (np.random.RandomState(1).rand(*SHAPE) * 100).astype(np.int16)
        self.volumeNode = slicer.util.addVolumeFromArray(image, name="ScissorsSliceCutTestVolume")
        self.volumeNode.SetSpacing(1.0, 1.0, sliceSpacing)
        self.volumeNode.SetOrigin(-31.7, 12.3, 5.0)
        self.segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode")
        self.segmentationNode.CreateDefaultDisplayNodes()
        self.segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(self.volumeNode)
        self.segmentId = self.segmentationNode.GetSegmentation().AddEmptySegment("Lesion")

        self.editor = slicer.qMRMLSegmentEditorWidget()
        self.editor.setMRMLScene(slicer.mrmlScene)
        self.editorNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentEditorNode")
        self.editor.setMRMLSegmentEditorNode(self.editorNode)
        self.editor.setSegmentationNode(self.segmentationNode)
        self.editor.setSourceVolumeNode(self.volumeNode)
        self.editor.setCurrentSegmentID(self.segmentId)
        self.editorNode.SetMaskMode(slicer.vtkMRMLSegmentationNode.EditAllowedEverywhere)
        self.editorNode.SetOverwriteMode(slicer.vtkMRMLSegmentEditorNode.OverwriteNone)

        layoutManager = slicer.app.layoutManager()
        layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutOneUpRedSliceView)
        self.redWidget = layoutManager.sliceWidget("Red")
        self.redWidget.mrmlSliceNode().SetOrientationToAxial()
        slicer.util.setSliceViewerLayers(background=self.volumeNode, fit=True)
        slicer.app.processEvents()

    def setSegment(self, array):
        slicer.util.updateSegmentBinaryLabelmapFromArray(array.astype(np.uint8), self.segmentationNode, self.segmentId, self.volumeNode)

    def segmentArray(self):
        return slicer.util.arrayFromSegmentBinaryLabelmap(self.segmentationNode, self.segmentId, self.volumeNode) > 0

    def ras(self, ijk):
        ijkToRas = vtk.vtkMatrix4x4()
        self.volumeNode.GetIJKToRASMatrix(ijkToRas)
        return ijkToRas.MultiplyPoint(list(ijk) + [1.0])[:3]

    def xy(self, ijk):
        rasToXy = vtk.vtkMatrix4x4()
        vtk.vtkMatrix4x4.Invert(self.redWidget.mrmlSliceNode().GetXYToRAS(), rasToXy)
        point = rasToXy.MultiplyPoint(list(self.ras(ijk)) + [1.0])
        return (round(point[0]), round(point[1]))

    def cut(self, operation, depthMm):
        """Rectangle Scissors on slice CUT_SLICE, enclosing the whole box, with Slice cut = Symmetric."""
        slicer.vtkMRMLSliceNode.JumpAllSlices(slicer.mrmlScene, *self.ras((31.5, 31.5, CUT_SLICE)), slicer.vtkMRMLSliceNode.CenteredJumpSlice)
        slicer.app.processEvents()
        self.editor.setActiveEffectByName("Scissors")
        effect = self.editor.activeEffect()
        for name, value in (("Operation", operation), ("Shape", "Rectangle"), ("SliceCutMode", "Symmetric"),
                            ("SliceCutDepthMm", depthMm), ("ApplyToAllVisibleSegments", 0)):
            effect.setParameter(name, str(value))
        (i0, j0), (i1, j1) = RECTANGLE_CORNERS_IJ
        slicer.util.clickAndDrag(self.redWidget, start=self.xy((i0, j0, CUT_SLICE)), end=self.xy((i1, j1, CUT_SLICE)), steps=5)
        slicer.app.processEvents()
        self.editor.setActiveEffectByName("")
        return self.segmentArray()

    def slabSlices(self, depthMm):
        """Slices that Erase inside cuts: this is the slab that the Symmetric slice cut covers."""
        self.setSegment(np.ones(SHAPE, bool))
        result = self.cut("EraseInside", depthMm)
        return [k for k in range(SHAPE[0]) if not result[k].all()]

    #
    # Tests
    #

    def test_outside_operations_change_only_the_slab(self):
        box = np.zeros(SHAPE, bool)
        box[BOX] = True
        for sliceSpacing, depths in ((1.0, (0.0, 1.0, 2.0, 3.0, 4.0, 2.5)), (2.0, (4.0, 6.0, 8.0))):
            self.createScene(sliceSpacing)
            for depthMm in depths:
                with self.subTest(sliceSpacing=sliceSpacing, depthMm=depthMm):
                    slab = self.slabSlices(depthMm)
                    self.assertIn(CUT_SLICE, slab)
                    self.assertEqual(slab, list(range(slab[0], slab[-1] + 1)))

                    # Erase outside: the rectangle encloses the box, so nothing may be erased
                    self.setSegment(box)
                    result = self.cut("EraseOutside", depthMm)
                    lostSlices = [k for k in range(SHAPE[0]) if (box[k] & ~result[k]).any()]
                    self.assertEqual(lostSlices, [], f"Erase outside erased voxels inside the rectangle (slab {slab})")
                    self.assertEqual(int(result.sum()), int(box.sum()))

                    # Fill outside: only the slab slices are filled, and only outside the rectangle
                    self.setSegment(np.zeros(SHAPE, bool))
                    result = self.cut("FillOutside", depthMm)
                    filledSlices = [k for k in range(SHAPE[0]) if result[k].any()]
                    self.assertEqual(filledSlices, slab)
                    self.assertEqual(int(result[BOX].sum()), 0, "Fill outside filled voxels inside the rectangle")


if __name__ == "__main__":
    unittest.main()

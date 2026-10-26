import qt

import slicer


class AbstractScriptedSegmentEditorEffect:
    """Abstract scripted segment editor effects for effects implemented in python.

    An example of generic effect is ``ThresholdEffect``.

    To use the effect, follow these steps:

    1. Instantiation and registration

      Instantiate segment editor effect adaptor class from
      module (e.g. from setup function), and set python source::

        import qSlicerSegmentationsEditorEffectsPythonQt as effects
        scriptedEffect = effects.qSlicerSegmentEditorScriptedEffect(None)
        scriptedEffect.setPythonSource(MyEffect.filePath)
        scriptedEffect.self().register()

      If effect name is added to ``slicer.modules.segmenteditorscriptedeffectnames``
      list then the above instantiation and registration steps are not necessary,
      as the `SegmentEditor` module do all these.

    2. Call host C++ implementation using::

        self.scriptedEffect.functionName()

    2.a. Most frequently used methods are:

      - Parameter get/set: ``parameter``, ``integerParameter``, ``doubleParameter``, ``setParameter``
      - Add options widget: ``addOptionsWidget``
      - Coordinate transforms: ``rasToXy``, ``xyzToRas``, ``xyToRas``, ``xyzToIjk``, ``xyToIjk``
      - Convenience getters: ``renderWindow``, ``renderer``, ``viewNode``

    2.b. Always call API functions (the ones that are defined in the adaptor \
         class ``qSlicerSegmentEditorScriptedEffect``) using the adaptor accessor ``self.scriptedEffect``.

      For example::

        self.scriptedEffect.updateGUIFromMRML()

    3. To prevent deactivation of an effect by clicking place fiducial toolbar button, \
       override ``interactionNodeModified(self, interactionNode)``
    """

    def __init__(self, scriptedEffect):
        self.scriptedEffect = scriptedEffect

    def register(self):
        effectFactorySingleton = slicer.qSlicerSegmentEditorEffectFactory.instance()
        effectFactorySingleton.registerEffect(self.scriptedEffect)

    def cleanup(self):
        """
        Clean up resources, event observers, and Qt signal/slot connections
        to ensure proper object deletion.

        Subclasses should override this method to disconnect any subclass-specific
        signals and slots. This method should be called before the object is
        garbage collected or explicitly deleted on the C++ side.

        Failing to disconnect signals/slots may prevent the object from being
        garbage collected, leading to memory leaks.

        For more details, see: https://github.com/Slicer/Slicer/issues/7392
        """
        pass

    #
    # Utility functions for convenient coordinate transformations
    #
    def rasToXy(self, ras, viewWidget):
        rasVector = qt.QVector3D(ras[0], ras[1], ras[2])
        xyPoint = self.scriptedEffect.rasToXy(rasVector, viewWidget)
        return [xyPoint.x(), xyPoint.y()]

    def xyzToRas(self, xyz, viewWidget):
        xyzVector = qt.QVector3D(xyz[0], xyz[1], xyz[2])
        rasVector = self.scriptedEffect.xyzToRas(xyzVector, viewWidget)
        return [rasVector.x(), rasVector.y(), rasVector.z()]

    def xyToRas(self, xy, viewWidget):
        xyPoint = qt.QPoint(xy[0], xy[1])
        rasVector = self.scriptedEffect.xyToRas(xyPoint, viewWidget)
        return [rasVector.x(), rasVector.y(), rasVector.z()]

    def xyzToIjk(self, xyz, viewWidget, image, parentTransformNode=None):
        xyzVector = qt.QVector3D(xyz[0], xyz[1], xyz[2])
        ijkVector = self.scriptedEffect.xyzToIjk(xyzVector, viewWidget, image, parentTransformNode)
        return [int(ijkVector.x()), int(ijkVector.y()), int(ijkVector.z())]

    def xyToIjk(self, xy, viewWidget, image, parentTransformNode=None):
        xyPoint = qt.QPoint(xy[0], xy[1])
        ijkVector = self.scriptedEffect.xyToIjk(xyPoint, viewWidget, image, parentTransformNode)
        return [int(ijkVector.x()), int(ijkVector.y()), int(ijkVector.z())]

    def setWidgetMinMaxStepFromImageSpacing(self, spinbox, imageData):
        # Set spinbox minimum, maximum, and step size from vtkImageData spacing:
        # Set widget minimum spacing and step size to be 1/10th or less than minimum spacing
        # Set widget minimum spacing to be 100x or more than minimum spacing
        if not imageData:
            return
        import math
        try:
            spinbox.unitAwareProperties = spinbox.unitAwareProperties & ~(slicer.qMRMLSpinBox.MinimumValue | slicer.qMRMLSpinBox.MaximumValue | slicer.qMRMLSpinBox.Precision)
        except TypeError:
            # Qt 6: the flags property is an opaque object that supports no operator. A spin box has Prefix, Suffix,
            # Precision, MinimumValue and MaximumValue on by default (qMRMLSpinBox.cxx): switch off the last three
            spinbox.unitAwareProperties = slicer.qMRMLSpinBox.Prefix | slicer.qMRMLSpinBox.Suffix
        stepSize = 10 ** (math.floor(math.log10(min(imageData.GetSpacing()) / 10.0)))
        spinbox.minimum = stepSize
        spinbox.maximum = 10 ** (math.ceil(math.log10(max(imageData.GetSpacing()) * 100.0)))
        spinbox.singleStep = stepSize
        # number of decimals is set to be able to show the step size (e.g., stepSize = 0.01 => decimals = 2)
        spinbox.decimals = max(int(-math.floor(math.log10(stepSize))), 0)

    def segmentSpacing(self):
        """Get voxel spacing of the labelmaps that the effect works on (selectedSegmentLabelmap(), defaultModifierLabelmap()).

        The spacing is read from the reference geometry, which is much faster than getting it from
        selectedSegmentLabelmap(), as no voxels need to be computed.
        Returns (1.0, 1.0, 1.0) if the reference geometry is not available.
        """
        referenceGeometryImage = self.scriptedEffect.referenceGeometryImage()
        if referenceGeometryImage is None:
            return (1.0, 1.0, 1.0)
        return referenceGeometryImage.GetSpacing()

    def croppedSelectedSegmentLabelmap(self, marginVoxels=0, singlePrecisionCoordinates=False):
        """Get the selected segment labelmap, cropped to the extent of the segment's non-empty region plus a margin.

        selectedSegmentLabelmap() covers the entire reference geometry, therefore processing it is slow
        on large images, even if the segment is small. Effects that process the whole segment and write the result
        back using ModificationModeSet (which clears the segment everywhere before setting the new content)
        can process this cropped labelmap instead and get the same result, if the margin is large enough to contain
        all the voxels that the processing may change and all the voxels that these changes depend on.

        :param marginVoxels: margin size in voxels, a single number or one number for each IJK axis.
          The cropped extent is clamped to the extent of the reference geometry.
        :param singlePrecisionCoordinates: set it to True if the processing computes single-precision floating-point
          coordinates from voxel indices relative to the start of the image (for example, the distance map in vtkITKImageMargin).
          Along axes where these coordinates are not exact (because of the voxel spacing), the start of the extent
          is not cropped, so that results are bit-identical to processing the labelmap that is not cropped.
        :return: cropped labelmap, which has the same origin, spacing, and axis directions as selectedSegmentLabelmap().
          If the segment is empty then the labelmap returned by selectedSegmentLabelmap() is returned.
        """
        selectedSegmentLabelmap = self.scriptedEffect.selectedSegmentLabelmap()
        if selectedSegmentLabelmap is None:
            return None
        effectiveExtent = [0, -1, 0, -1, 0, -1]
        if not slicer.vtkOrientedImageDataResample.CalculateEffectiveExtent(selectedSegmentLabelmap, effectiveExtent):
            # Empty segment
            return selectedSegmentLabelmap
        try:
            marginPerAxis = [int(marginVoxels[axis]) for axis in range(3)]
        except TypeError:
            marginPerAxis = [int(marginVoxels)] * 3
        wholeExtent = selectedSegmentLabelmap.GetExtent()
        croppedExtent = [0, -1, 0, -1, 0, -1]
        spacing = selectedSegmentLabelmap.GetSpacing()
        for axis in range(3):
            if singlePrecisionCoordinates and not self._isSinglePrecisionVoxelCoordinateExact(
                    spacing[axis], wholeExtent[axis * 2 + 1] - wholeExtent[axis * 2] + 1):
                croppedExtent[axis * 2] = wholeExtent[axis * 2]
            else:
                croppedExtent[axis * 2] = max(wholeExtent[axis * 2], effectiveExtent[axis * 2] - marginPerAxis[axis])
            croppedExtent[axis * 2 + 1] = min(wholeExtent[axis * 2 + 1], effectiveExtent[axis * 2 + 1] + marginPerAxis[axis])
        if croppedExtent == list(wholeExtent):
            return selectedSegmentLabelmap
        croppedLabelmap = slicer.vtkOrientedImageData()
        slicer.vtkOrientedImageDataResample.CopyImage(selectedSegmentLabelmap, croppedLabelmap, croppedExtent)
        return croppedLabelmap

    @staticmethod
    def _isSinglePrecisionVoxelCoordinateExact(spacing, numberOfVoxels):
        """Check if index * spacing is exactly representable as single-precision floating-point number for all voxel indices.

        Then the coordinate differences between voxels do not depend on the voxel positions.
        """
        import math
        mantissa, _ = math.frexp(spacing)
        significand = int(mantissa * 2**53)  # spacing = significand * 2**k for some integer k (exactly)
        if significand == 0:
            return True
        while significand % 2 == 0:
            significand //= 2
        # products of indices and the odd significand must fit in the 24-bit single-precision significand
        return (numberOfVoxels - 1) * significand < 2**24

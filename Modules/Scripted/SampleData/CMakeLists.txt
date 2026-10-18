
#-----------------------------------------------------------------------------
set(MODULE_NAME SampleData)

#-----------------------------------------------------------------------------
set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  )

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/BaselineVolume.png
  Resources/Icons/CTACardio.png
  Resources/Icons/CTCardioSeq.png
  Resources/Icons/CTPCardioSeq.png
  Resources/Icons/CTChest.png
  Resources/Icons/CTLiver.png
  Resources/Icons/DTIBrain.png
  Resources/Icons/DTIVolume.png
  Resources/Icons/dwi.png
  Resources/Icons/DZ-CBCT.png
  Resources/Icons/MRBrainTumor1.png
  Resources/Icons/MRBrainTumor2.png
  Resources/Icons/MRHead.png
  Resources/Icons/MRProstate.png
  Resources/Icons/Panoramix-cropped.png
  Resources/Icons/PreDentalSurgery.png
  Resources/Icons/CTBrain.png
  Resources/Icons/TinyPatient.png
  Resources/Icons/MNI152NLin2009cAsym.png
  Resources/Icons/MNI152NLin6Asym.png
  Resources/Data/tpl-MNI152NLin2009cAsym_res-02_desc-brain_T1w.nii.gz
  Resources/Data/tpl-MNI152NLin2009cAsym_res-02_desc-brain_mask.nii.gz
  Resources/Data/LICENSE-MNI152NLin2009cAsym.txt
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )

#-----------------------------------------------------------------------------
if(BUILD_TESTING)

  # Register the unittest subclass in the main script as a ctest.
  # Note that the test will also be available at runtime.
  slicer_add_python_unittest(SCRIPT ${MODULE_NAME}.py)

endif()

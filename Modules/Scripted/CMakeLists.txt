set(modules
  CropVolumeSequence
  DataProbe
  ImportItkSnapLabel
  PerformanceTests
  SampleData
  ScreenCapture
  SegmentEditor
  SegmentStatistics
  SelfTests
  Studio
  VectorToScalarVolume
  WebServer
  )
if(Slicer_BUILD_EXTENSIONMANAGER_SUPPORT)
  list(APPEND modules
    ExtensionWizard
    )
endif()
if(Slicer_USE_NUMPY)
  list(APPEND modules
    Endoscopy
    )
endif()
if(Slicer_USE_SCIPY)
  list(APPEND modules
    LesionNetworkMapping
    LineProfile
    )
endif()
if(Slicer_BUILD_MULTIVOLUME_SUPPORT)
  list(APPEND modules
    MultiVolumeImporter
    )
endif()
if(Slicer_BUILD_DICOM_SUPPORT)
  list(APPEND modules
    DICOM
    DICOMLib
    DICOMPlugins
    DICOMPatcher
    )
endif()

include(SlicerCheckModuleEnabled)

foreach(module ${modules})
  slicer_is_scripted_builtin_module_enabled(${module} _build_module)
  if(_build_module)
    add_subdirectory(${module})
  endif()
endforeach()

# The lesion network mapping library (LesionNetworkMappingLib, pure Python) is installed even when the
# module user interface is disabled, so that "PythonSlicer -m LesionNetworkMappingLib.engine" works.
if(Slicer_USE_NUMPY)
  slicer_is_scripted_builtin_module_enabled(LesionNetworkMapping _build_lesion_network_mapping)
  if(NOT Slicer_USE_SCIPY OR NOT _build_lesion_network_mapping)
    set(LesionNetworkMapping_LIBRARY_ONLY TRUE)
    add_subdirectory(LesionNetworkMapping)
  endif()
endif()

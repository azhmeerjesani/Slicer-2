
set(Slicer_PYTHON_SCRIPTS
  slicer/__init__
  slicer/cli
  slicer/i18n
  slicer/logic
  slicer/parameterNodeWrapper/__init__
  slicer/parameterNodeWrapper/default
  slicer/parameterNodeWrapper/guiCreation
  slicer/parameterNodeWrapper/guiConnectors
  slicer/parameterNodeWrapper/parameterInfo
  slicer/parameterNodeWrapper/parameterPack
  slicer/parameterNodeWrapper/serializers
  slicer/parameterNodeWrapper/types
  slicer/parameterNodeWrapper/util
  slicer/parameterNodeWrapper/validators
  slicer/parameterNodeWrapper/wrapper
  slicer/packaging
  slicer/ScriptedLoadableModule
  slicer/ScriptedFileIO/__init__
  slicer/ScriptedFileIO/Legacy
  slicer/ScriptedFileIO/ReaderWriter
  slicer/ScriptedFileIO/Registration
  slicer/testing
  slicer/util
  slicerqt
  mrml
  vtkAddon
  vtkITK
  vtkSegmentationCore
  vtkTeem
  )

set(Slicer_PYTHON_MODULES_CONFIG "
# slicer libs
'mrml',
'vtkAddon',
'vtkSegmentationCore',
'qMRMLWidgetsPythonQt',
# slicer base libs
'logic',
'qSlicerBaseQTCorePythonQt',
'qSlicerBaseQTCLIPythonQt',
'qSlicerBaseQTGUIPythonQt',
'qSlicerBaseQTAppPythonQt'"
  )

configure_file(
  slicer/kits.py.in
  ${CMAKE_CURRENT_BINARY_DIR}/slicer/kits.py
  @ONLY
  )

list(APPEND Slicer_PYTHON_SCRIPTS ${CMAKE_CURRENT_BINARY_DIR}/slicer/kits.py)

set(Slicer_PYTHON_RESOURCES
  )

if(Slicer_USE_SimpleITK)
  list(APPEND Slicer_PYTHON_SCRIPTS
    sitkUtils
    )
endif()

ctkMacroCompilePythonScript(
  TARGET_NAME SlicerBase
  SCRIPTS "${Slicer_PYTHON_SCRIPTS}"
  RESOURCES "${Slicer_PYTHON_RESOURCES}"
  DESTINATION_DIR ${Slicer_BINARY_DIR}/bin/Python
  INSTALL_DIR ${Slicer_INSTALL_BIN_DIR}
  )

# Generate a lazily-loading replacement for the eager "vtk" module distributed
# with VTK. It is placed in ${Slicer_BINARY_DIR}/bin/Python, which precedes the
# VTK site-packages directory on the Python path, so "import vtk" resolves to
# the generated module and each attribute access imports only the vtkmodules
# submodule that provides it. This saves several hundred milliseconds at every
# application startup. The module is generated from vtk.py.in by
# Utilities/Scripts/GenerateLazyVtkModule.py, which fills in the attribute index.
set(_lazy_vtk_module_template ${CMAKE_CURRENT_SOURCE_DIR}/vtk.py.in)
set(_lazy_vtk_module ${Slicer_BINARY_DIR}/bin/Python/vtk.py)
# The VTK Python package location is passed explicitly: during a fresh
# superbuild the launcher settings of ${PYTHON_EXECUTABLE} may not
# reference the VTK build tree yet when this rule runs.
set(_lazy_vtk_pythonpath "")
if(DEFINED VTK_PYTHONPATH_LAUNCHER_BUILD)
  set(_lazy_vtk_pythonpath ${VTK_PYTHONPATH_LAUNCHER_BUILD})
endif()
add_custom_command(
  OUTPUT ${_lazy_vtk_module}
  COMMAND ${CMAKE_COMMAND} -E env "PYTHONPATH=${_lazy_vtk_pythonpath}"
    ${PYTHON_EXECUTABLE}
    ${Slicer_SOURCE_DIR}/Utilities/Scripts/GenerateLazyVtkModule.py
    --template ${_lazy_vtk_module_template}
    --output ${_lazy_vtk_module}
  DEPENDS
    ${Slicer_SOURCE_DIR}/Utilities/Scripts/GenerateLazyVtkModule.py
    ${_lazy_vtk_module_template}
  COMMENT "Generating lazily-loading vtk module"
  VERBATIM
  )
add_custom_target(SlicerGenerateLazyVtkModule ALL
  DEPENDS ${_lazy_vtk_module}
  )
install(FILES ${_lazy_vtk_module}
  DESTINATION ${Slicer_INSTALL_BIN_DIR}/Python
  COMPONENT Runtime
  )

# Generate the class index of the loadable-module Python wrappers, next to them
# in the qt-loadable-modules directory. With the setting Studio/LazyPythonWrappers,
# qSlicerScriptedUtils::importModulePythonExtensions registers this index instead
# of importing every wrapper at the first module instantiation, and each wrapper is
# imported on first use (see slicer.util._registerLazyWrapperIndex). The index is
# read from the wrapper libraries by Utilities/Scripts/GenerateSlicerWrapperIndex.py,
# which needs no Slicer module, so it can run in the bare build environment.
#
# The wrapper targets are defined by the modules, which are added after this
# directory, so the rule is created at the end of the top-level directory.
set(Slicer_PYTHON_WRAPPER_INDEX_FILENAME "SlicerWrapperIndex.json")
set(_wrapper_index ${CMAKE_BINARY_DIR}/${Slicer_QTLOADABLEMODULES_LIB_DIR}/${Slicer_PYTHON_WRAPPER_INDEX_FILENAME})
set_property(GLOBAL PROPERTY _SLICER_PYTHON_WRAPPER_INDEX ${_wrapper_index})

function(_slicer_add_python_wrapper_index_target)
  get_property(_wrapper_index GLOBAL PROPERTY _SLICER_PYTHON_WRAPPER_INDEX)
  get_property(_slicer_targets GLOBAL PROPERTY Slicer_TARGETS)
  # Rerun when a wrapper library is rebuilt (file-level dependency on each target)
  set(_wrapper_targets)
  foreach(_target IN LISTS _slicer_targets)
    foreach(_candidate ${_target} ${_target}Python ${_target}PythonQt)
      if(TARGET ${_candidate}
          AND (_candidate MATCHES "^vtkSlicer.*Module(Logic|MRML|MRMLDisplayableManager|VTKWidgets)Python$"
            OR _candidate MATCHES "^qSlicer.*PythonQt$"))
        list(APPEND _wrapper_targets ${_candidate})
      endif()
    endforeach()
  endforeach()
  list(REMOVE_DUPLICATES _wrapper_targets)
  get_filename_component(_wrapper_dir ${_wrapper_index} DIRECTORY)
  add_custom_command(
    OUTPUT ${_wrapper_index}
    COMMAND ${PYTHON_EXECUTABLE}
      ${Slicer_SOURCE_DIR}/Utilities/Scripts/GenerateSlicerWrapperIndex.py
      --directory ${_wrapper_dir}
      --output ${_wrapper_index}
    DEPENDS
      ${Slicer_SOURCE_DIR}/Utilities/Scripts/GenerateSlicerWrapperIndex.py
      ${_wrapper_targets}
    COMMENT "Generating the class index of the loadable-module Python wrappers"
    VERBATIM
    )
  add_custom_target(SlicerGeneratePythonWrapperIndex ALL
    DEPENDS ${_wrapper_index}
    )
  # ${PYTHON_EXECUTABLE} is a launcher whose settings are written after the
  # application links (see SlicerGenerateLazyVtkModule in the top-level CMakeLists.txt).
  if(TARGET ${Slicer_MAIN_PROJECT_APPLICATION_NAME}ConfigureLauncher)
    add_dependencies(SlicerGeneratePythonWrapperIndex
      ${Slicer_MAIN_PROJECT_APPLICATION_NAME}ConfigureLauncher)
  endif()
endfunction()
cmake_language(DEFER DIRECTORY ${Slicer_SOURCE_DIR} CALL _slicer_add_python_wrapper_index_target)

install(FILES ${_wrapper_index}
  DESTINATION ${Slicer_INSTALL_QTLOADABLEMODULES_LIB_DIR}
  COMPONENT RuntimeLibraries
  )

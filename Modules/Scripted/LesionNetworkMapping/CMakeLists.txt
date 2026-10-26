#-----------------------------------------------------------------------------
set(MODULE_NAME LesionNetworkMapping)

#-----------------------------------------------------------------------------
# The library (pure Python), the user interface plug-ins and their data files are globbed so that
# files added later (for example LesionNetworkMappingUI/card_report.py) are picked up without
# editing this file. Relative paths keep the package folders in the build and install trees.
file(GLOB MODULE_PYTHON_LIB_SCRIPTS CONFIGURE_DEPENDS
  RELATIVE ${CMAKE_CURRENT_SOURCE_DIR}
  ${MODULE_NAME}Lib/*.py
  )

file(GLOB MODULE_PYTHON_LIB_RESOURCES CONFIGURE_DEPENDS
  RELATIVE ${CMAKE_CURRENT_SOURCE_DIR}
  ${MODULE_NAME}Lib/catalog/*.json
  ${MODULE_NAME}Lib/recipes/*.json
  )

if(LesionNetworkMapping_LIBRARY_ONLY)
  # The module user interface is disabled: install the library only (see Modules/Scripted/CMakeLists.txt)
  message(STATUS "Configuring Python library: ${MODULE_NAME}Lib")
  ctkMacroCompilePythonScript(
    TARGET_NAME ${MODULE_NAME}Lib
    SCRIPTS "${MODULE_PYTHON_LIB_SCRIPTS}"
    RESOURCES "${MODULE_PYTHON_LIB_RESOURCES}"
    DESTINATION_DIR ${CMAKE_BINARY_DIR}/${Slicer_QTSCRIPTEDMODULES_LIB_DIR}
    INSTALL_DIR ${Slicer_INSTALL_QTSCRIPTEDMODULES_LIB_DIR}
    NO_INSTALL_SUBDIR
    )
  return()
endif()

file(GLOB MODULE_PYTHON_UI_SCRIPTS CONFIGURE_DEPENDS
  RELATIVE ${CMAKE_CURRENT_SOURCE_DIR}
  ${MODULE_NAME}UI/*.py
  )

set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  ${MODULE_PYTHON_LIB_SCRIPTS}
  ${MODULE_PYTHON_UI_SCRIPTS}
  )

set(MODULE_PYTHON_RESOURCES
  Resources/Icons/${MODULE_NAME}.svg
  ${MODULE_PYTHON_LIB_RESOURCES}
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

  # Additional build-time testing (the library tests, run with PythonSlicer)
  add_subdirectory(Testing)
endif()

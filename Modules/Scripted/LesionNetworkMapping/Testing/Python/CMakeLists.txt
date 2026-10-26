#-----------------------------------------------------------------------------
# One test runs every library test file (Testing/Python/LesionNetworkMapping*Test.py) with
# PythonSlicer from the module folder, so that later work packages add test files without editing
# this file. Test files that need the Slicer application raise unittest.SkipTest when it is absent.
if(Slicer_USE_NUMPY AND PYTHON_EXECUTABLE)
  add_test(
    NAME py_LesionNetworkMappingLibTests
    COMMAND ${Slicer_LAUNCH_COMMAND} ${PYTHON_EXECUTABLE} -m unittest discover
      -s Testing/Python
      -p "LesionNetworkMapping*Test.py"
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/../..
    )
  set_property(TEST py_LesionNetworkMappingLibTests PROPERTY LABELS ${MODULE_NAME})
endif()

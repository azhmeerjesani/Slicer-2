# The Studio shell needs the main window, therefore the tests do not use --no-main-window.
# Every Studio*Test.py of this folder is registered: a new test file is picked up without editing this file
# (CONFIGURE_DEPENDS re-runs CMake when a file is added or removed).
file(GLOB _studio_tests RELATIVE ${CMAKE_CURRENT_SOURCE_DIR} CONFIGURE_DEPENDS Studio*Test.py)
foreach(_test ${_studio_tests})
  slicer_add_python_unittest(SCRIPT ${_test})
endforeach()

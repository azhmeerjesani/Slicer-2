################################################################################
#
#  Program: 3D Slicer
#
#  Copyright (c) Kitware Inc.
#
#  See COPYRIGHT.txt
#  or http://www.slicer.org/copyright/copyright.txt for details.
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#  This file was originally developed by Jean-Christophe Fillion-Robin, Kitware Inc.
#  and was partially funded by NIH grant 3P41RR013218-12S1
#
################################################################################

macro(__SlicerBlockFindQtAndCheckVersion_find_qt)
  if(Slicer_REQUIRED_QT_VERSION VERSION_GREATER_EQUAL "6")
    find_package(Qt6 COMPONENTS ${Slicer_REQUIRED_QT_MODULES})
    set(_found_var Qt6_FOUND)
    set(_error_details "You probably need to set the Qt6_DIR variable.")
    set(_major ${Qt6_VERSION_MAJOR})
    set(_minor ${Qt6_VERSION_MINOR})
    set(_patch ${Qt6_VERSION_PATCH})
    set(_found_prefix "Qt6")
  else()
    find_package(Qt5 COMPONENTS ${Slicer_REQUIRED_QT_MODULES})
    set(_found_var Qt5_FOUND)
    set(_error_details "You probably need to set the Qt5_DIR variable.")
    set(_major ${Qt5_VERSION_MAJOR})
    set(_minor ${Qt5_VERSION_MINOR})
    set(_patch ${Qt5_VERSION_PATCH})
    set(_found_prefix "Qt5")
  endif()
  if(NOT ${_found_var})
    message(FATAL_ERROR "error: Qt ${Slicer_REQUIRED_QT_VERSION} was not found on your system."
                        "${_error_details}")
  endif()

  # Check version
  if("${_major}.${_minor}.${_patch}" VERSION_LESS "${Slicer_REQUIRED_QT_VERSION}")
    message(FATAL_ERROR "error: Slicer requires at least Qt ${Slicer_REQUIRED_QT_VERSION} "
                        "-- you cannot use Qt ${_major}.${_minor}.${_patch}. ${extra_error_message}")
  endif()

  # MSVC 1951 (VS 2022 17.11) introduced ABI changes that require Qt 5.15.17 or newer.
  if(WIN32 AND MSVC_VERSION VERSION_GREATER_EQUAL 1951)
    if("${_major}.${_minor}.${_patch}" VERSION_LESS "5.15.17")
      message(FATAL_ERROR "error: MSVC toolset version ${MSVC_VERSION} requires that Qt 5 is 5.15.17 or newer "
                          "-- you cannot use Qt ${_major}.${_minor}.${_patch} with this compiler.")
    endif()
  endif()

  set(command_separated_module_list)
  # Check if all expected Qt modules have been discovered
  foreach(module ${Slicer_REQUIRED_QT_MODULES})
    if(NOT "${${_found_prefix}${module}_FOUND}")
      message(FATAL_ERROR "error: Missing Qt module named \"${module}\"")
    endif()
    set(command_separated_module_list "${command_separated_module_list}${module}, ")
  endforeach()
endmacro()

# Sanity checks - Check if variable are defined
set(expected_defined_vars
  Slicer_REQUIRED_QT_VERSION
  Slicer_REQUIRED_QT_MODULES
  )
foreach(v ${expected_defined_vars})
  if(NOT DEFINED ${v})
    message(FATAL_ERROR "error: ${v} CMake variable is not defined.")
  endif()
endforeach()

# Check that either Qt5_DIR or Qt6_DIR is provided by CTK
set(extra_error_message)
  if(DEFINED CTK_Qt5_DIR AND NOT EXISTS "${CTK_Qt5_DIR}")
    message(FATAL_ERROR "error: You should probably re-configure CTK. CTK_Qt5_DIR points to a nonexistent directory: ${CTK_Qt5_DIR}")
  endif()
if(DEFINED CTK_Qt6_DIR AND NOT EXISTS "${CTK_Qt6_DIR}")
  message(FATAL_ERROR "error: You should probably re-configure CTK. CTK_Qt6_DIR points to a nonexistent directory: ${CTK_Qt6_DIR}")
endif()

#-----------------------------------------------------------------------------
# Keep every Qt 6 CMake package (in particular the Qt6<Module>Tools packages that
# define Qt6::qmake, Qt6::moc, Qt6::rcc and Qt6::uic) in the Qt installation
# selected with Qt6_DIR (see SlicerMacroIsolateQt6Packages.cmake). The top-level
# CMakeLists.txt already calls this before its first find_package(Qt6), which is
# what creates the Qt6::qmake, Qt6::moc and Qt6::rcc targets.
include("${CMAKE_CURRENT_LIST_DIR}/SlicerMacroIsolateQt6Packages.cmake")
slicer_isolate_qt6_packages()

__SlicerBlockFindQtAndCheckVersion_find_qt()

slicer_restore_qt6_prefix_path()

set(_project_name ${Slicer_MAIN_PROJECT_APPLICATION_NAME})
if(NOT Slicer_SOURCE_DIR)
  set(_project_name ${PROJECT_NAME})
endif()
message(STATUS "Configuring ${_project_name} with Qt ${_major}.${_minor}.${_patch} (using modules: ${command_separated_module_list})")

if(NOT _slicer_qt_cmake_dir STREQUAL "")
  # Every Qt package must come from the selected Qt and have its version.
  get_cmake_property(_slicer_qt_cache_vars CACHE_VARIABLES)
  set(_slicer_qt_mismatches)
  foreach(_var IN LISTS _slicer_qt_cache_vars)
    if(NOT _var MATCHES "^(Qt6[A-Za-z0-9]+)_DIR$" OR _var STREQUAL "Qt6_DIR")
      continue()
    endif()
    set(_pkg "${CMAKE_MATCH_1}")
    set(_value "${${_var}}")
    get_filename_component(_value_name "${_value}" NAME)
    if(NOT _value_name STREQUAL _pkg)
      continue()
    endif()
    get_filename_component(_value_parent "${_value}/.." ABSOLUTE)
    if(NOT _value_parent STREQUAL _slicer_qt_cmake_dir)
      list(APPEND _slicer_qt_mismatches "${_var}=${_value}")
    elseif(DEFINED ${_pkg}_VERSION AND NOT "${${_pkg}_VERSION}" STREQUAL ""
        AND NOT "${${_pkg}_VERSION}" VERSION_EQUAL "${_major}.${_minor}.${_patch}")
      list(APPEND _slicer_qt_mismatches "${_pkg} ${${_pkg}_VERSION} (${_value})")
    endif()
  endforeach()
  # The tools must be in the selected Qt prefix (compared as given, and with
  # symlinks resolved: Homebrew links its kegs into one prefix).
  set(_slicer_qt_install_prefix "${_slicer_qt_prefix}")
  if(DEFINED QT6_INSTALL_PREFIX)
    set(_slicer_qt_install_prefix "${QT6_INSTALL_PREFIX}")
  endif()
  get_filename_component(_slicer_qt_install_prefix_real "${_slicer_qt_install_prefix}" REALPATH)
  foreach(_tool IN ITEMS qmake moc rcc uic)
    if(NOT TARGET Qt6::${_tool})
      continue()
    endif()
    get_target_property(_tool_location Qt6::${_tool} LOCATION)
    get_filename_component(_tool_location_abs "${_tool_location}" ABSOLUTE)
    get_filename_component(_tool_location_real "${_tool_location}" REALPATH)
    string(FIND "${_tool_location_abs}/" "${_slicer_qt_install_prefix}/" _pos)
    string(FIND "${_tool_location_real}/" "${_slicer_qt_install_prefix_real}/" _pos_real)
    if(NOT _pos EQUAL 0 AND NOT _pos_real EQUAL 0)
      list(APPEND _slicer_qt_mismatches "Qt6::${_tool}=${_tool_location}")
    endif()
  endforeach()
  if(_slicer_qt_mismatches)
    list(JOIN _slicer_qt_mismatches "\n  " _slicer_qt_mismatches_text)
    message(FATAL_ERROR "error: Qt ${_major}.${_minor}.${_patch} was selected with Qt6_DIR=${Qt6_DIR}, "
      "but these Qt packages or tools come from another Qt installation or version:\n  ${_slicer_qt_mismatches_text}\n"
      "Another Qt (for example Homebrew's) is found first through PATH, CMAKE_PREFIX_PATH or a system prefix. "
      "Imported Qt targets created by an earlier lookup are not redefined, so remove that Qt from PATH or put the "
      "selected Qt first in CMAKE_PREFIX_PATH, then configure a clean build tree "
      "(Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh --clean).")
  endif()
endif()

# Since neither Qt5 or Qt6 set CMake variables for plugins and binary directories,
# we explicitly set them here.
if(_major VERSION_GREATER_EQUAL "6" AND DEFINED QT6_INSTALL_PREFIX AND DEFINED QT6_INSTALL_PLUGINS)
  # Use the install layout of the Qt package selected with Qt6_DIR (QtInstallPaths.cmake,
  # the same answers as its "qmake -query") rather than the qmake that the Qt6::qmake
  # target names, which could belong to another Qt.
  set(QT_PLUGINS_DIR "${QT6_INSTALL_PREFIX}/${QT6_INSTALL_PLUGINS}")
  set(QT_BINARY_DIR "${QT6_INSTALL_PREFIX}/${QT6_INSTALL_BINS}")
  set(QT_LIBRARY_DIR "${QT6_INSTALL_PREFIX}/${QT6_INSTALL_LIBS}")
  message(STATUS "Setting QT_PLUGINS_DIR: ${QT_PLUGINS_DIR}")
  message(STATUS "Setting QT_BINARY_DIR: ${QT_BINARY_DIR}")
  message(STATUS "Setting QT_LIBRARY_DIR: ${QT_LIBRARY_DIR}")
  # Passed by the superbuild as CMAKE_PREFIX_PATH to the projects built with Qt
  # (CTK, PythonQt, VTK, ...), so that they find the Tools packages of this Qt too.
  if(NOT _slicer_qt_cmake_dir STREQUAL "")
    set(Slicer_QT_PREFIX_PATH "${_slicer_qt_cmake_dir}")
  else()
    set(Slicer_QT_PREFIX_PATH "${QT6_INSTALL_PREFIX}/${QT6_INSTALL_LIBS}/cmake")
  endif()

  # The qmake of the selected Qt must report the same version (it is the qmake
  # used by tools such as the PythonQt generator).
  set(_slicer_qmake "${QT_BINARY_DIR}/qmake${CMAKE_EXECUTABLE_SUFFIX}")
  if(EXISTS "${_slicer_qmake}" AND NOT CMAKE_CROSSCOMPILING)
    execute_process(
      COMMAND "${_slicer_qmake}" -query QT_VERSION
      OUTPUT_VARIABLE _slicer_qmake_version
      OUTPUT_STRIP_TRAILING_WHITESPACE
      RESULT_VARIABLE _slicer_qmake_result
      )
    if(_slicer_qmake_result EQUAL 0 AND NOT _slicer_qmake_version VERSION_EQUAL "${_major}.${_minor}.${_patch}")
      message(FATAL_ERROR "error: ${_slicer_qmake} reports Qt ${_slicer_qmake_version}, but Qt6_DIR=${Qt6_DIR} is Qt ${_major}.${_minor}.${_patch}")
    endif()
  endif()
else()
get_target_property(QMAKE_EXECUTABLE Qt${_major}::qmake LOCATION)

execute_process(
    COMMAND ${QMAKE_EXECUTABLE} -query QT_INSTALL_PLUGINS
    OUTPUT_VARIABLE QT_PLUGINS_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE qmake_result
)
if(NOT qmake_result EQUAL 0)
    message(FATAL_ERROR "Failed to get QT_PLUGINS_DIR using qmake")
endif()
message(STATUS "Setting QT_PLUGINS_DIR: ${QT_PLUGINS_DIR}")

execute_process(
    COMMAND ${QMAKE_EXECUTABLE} -query QT_INSTALL_BINS
    OUTPUT_VARIABLE QT_BINARY_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE qmake_result
)
if(NOT qmake_result EQUAL 0)
    message(FATAL_ERROR "Failed to get QT_BINARY_DIR using qmake")
endif()
message(STATUS "Setting QT_BINARY_DIR: ${QT_BINARY_DIR}")

execute_process(
    COMMAND ${QMAKE_EXECUTABLE} -query QT_INSTALL_LIBS
    OUTPUT_VARIABLE QT_LIBRARY_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE qmake_result
)
if(NOT qmake_result EQUAL 0)
    message(FATAL_ERROR "Failed to get QT_LIBRARY_DIR using qmake")
endif()
message(STATUS "Setting QT_LIBRARY_DIR: ${QT_LIBRARY_DIR}")
endif()

# Sanity checks
foreach(varname IN ITEMS QT_PLUGINS_DIR QT_BINARY_DIR QT_LIBRARY_DIR)
  if("${${varname}}" STREQUAL "" OR NOT EXISTS "${${varname}}")
    message(FATAL_ERROR "Variable ${varname} is expected to be set to an existing path [${${varname}}]")
  endif()
endforeach()

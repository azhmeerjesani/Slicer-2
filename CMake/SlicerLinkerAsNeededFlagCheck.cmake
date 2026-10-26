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
#  This file was originally developed by Sankhesh Jhaveri, Kitware Inc.
#  and was partially funded by NIH grant 3P41RR013218-12S1
#
################################################################################

#
# Slicer Linker --as-needed flag check
#
# Check if the linker will resolve symbols of underlinked libraries
#
# This script sets a boolean flag Slicer_LINKER_NO_AS_NEEDED_FLAG_REQUIRED
# to either true or false.
#
# It also provides the optional underlinking check Slicer_CHECK_UNDERLINKING (OFF by default).
# When ON, shared and module libraries are linked with -Wl,-z,defs, so that a library that uses
# a symbol without linking the library that defines it fails to link, instead of failing at
# run time. This matters when libraries are linked with -Wl,--as-needed
# (see Slicer_USE_LINKER_AS_NEEDED), because a dependency that is only reached transitively is
# then no longer recorded. To run the check, reconfigure and relink everything:
#
#   cmake -DSlicer_CHECK_UNDERLINKING:BOOL=ON <Slicer-build> && cmake --build <Slicer-build>
#
# then turn it OFF again. GCC builds already link with -Wl,--no-undefined
# (see SlicerBlockCXXRequiredFlags.cmake), which performs the same check.


message(STATUS "Checking if --no-as-needed linker flag is required")
set(Slicer_LINKER_NO_AS_NEEDED_FLAG_REQUIRED false)
set(LINK_TEST_SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/CMake/SlicerLinkerAsNeededFlagCheck)
set(LINK_TEST_BINARY_DIR ${CMAKE_CURRENT_BINARY_DIR}/SlicerLinkerAsNeededFlagCheck)
try_compile(Slicer_LINKER_LINKS_UNDERLINKED_LIBS
  ${LINK_TEST_BINARY_DIR}
  ${LINK_TEST_SOURCE_DIR}
  LINK_TEST
  )
if(Slicer_LINKER_LINKS_UNDERLINKED_LIBS)
  message(STATUS "Checking if --no-as-needed linker flag is required - no")
else()
  try_compile(Slicer_LINKER_NO_AS_NEEDED_LINKS_UNDERLINKED_LIBS
    ${LINK_TEST_BINARY_DIR}
    ${LINK_TEST_SOURCE_DIR}
    LINK_TEST_FLAGS
    CMAKE_FLAGS -DCMAKE_EXE_LINKER_FLAGS=-Wl,--no-as-needed
    )
  if(Slicer_LINKER_NO_AS_NEEDED_LINKS_UNDERLINKED_LIBS)
    message(STATUS "Checking if --no-as-needed linker flag is required - yes")
    set(Slicer_LINKER_NO_AS_NEEDED_FLAG_REQUIRED true)
  else()
    message(STATUS "Checking if --no-as-needed linker flag is required - failed.")
    message(WARNING "Could not compile test code."
      "Linker could fail trying to resolve symbols for underlinked libraries."
      "See issue 2321 (https://mantisarchive.slicer.org/view.php?id=2321) for more details.")
  endif()
endif()

option(Slicer_CHECK_UNDERLINKING "Link shared and module libraries with -Wl,-z,defs to detect underlinked libraries" OFF)
mark_as_advanced(Slicer_CHECK_UNDERLINKING)
mark_as_superbuild(Slicer_CHECK_UNDERLINKING:BOOL)
if(Slicer_CHECK_UNDERLINKING)
  message(STATUS "Checking underlinked libraries with -Wl,-z,defs")
  foreach(_linker_flags_var IN ITEMS CMAKE_SHARED_LINKER_FLAGS CMAKE_MODULE_LINKER_FLAGS)
    if(NOT " ${${_linker_flags_var}} " MATCHES " -Wl,-z,defs ")
      set(${_linker_flags_var} "${${_linker_flags_var}} -Wl,-z,defs")
    endif()
  endforeach()
endif()

################################################################################
#
#  Program: 3D Slicer
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
################################################################################

#-----------------------------------------------------------------------------
# slicer_isolate_qt6_packages()
#
# Keep every Qt 6 CMake package (in particular the Qt6<Module>Tools packages that
# define Qt6::qmake, Qt6::moc, Qt6::rcc and Qt6::uic) in the Qt installation
# selected with Qt6_DIR.
#
# Qt looks up its Tools packages without NO_DEFAULT_PATH, and their version files
# accept any newer version. A second Qt elsewhere on the machine (for example
# Homebrew's /opt/homebrew/lib/cmake, found through the "<prefix>/bin" entries of
# PATH, or the /usr/local system prefix of an Intel Mac) is then preferred to the
# selected one: the build used its moc/rcc/uic, and the qmake-derived
# QT_PLUGINS_DIR packaged its plugins (for example a Qt 6.12 libqcocoa next to Qt
# 6.11 frameworks, which stops the application at start-up with "Plugin uses
# incompatible Qt library").
#
# Call it right before a find_package(Qt6 ...) and slicer_restore_qt6_prefix_path()
# right after. It must run before the FIRST Qt 6 lookup of a configuration:
# find_package(Qt6 COMPONENTS Core) already loads Qt6CoreTools and creates the
# Qt6::qmake, Qt6::moc and Qt6::rcc imported targets, and a later lookup never
# redefines targets that already exist (the generated targets files return early).
#
# The macro
# - forgets cached Qt6<Package>_DIR entries that point outside the selected Qt
#   (a cached <Package>_DIR is used as is, whatever the search paths are), so that a
#   build tree configured earlier with another Qt repairs itself;
# - puts the CMake folder of the selected Qt first in CMAKE_PREFIX_PATH.
#
# It sets _slicer_qt_cmake_dir (empty when nothing was done), _slicer_qt_prefix and
# _slicer_qt_saved_prefix_path in the calling scope.
#
# Qt installs all its CMake packages in one directory (<libdir>/cmake). The
# paths are compared without resolving symlinks, because Homebrew links the
# packages of several kegs into one such directory.
macro(slicer_isolate_qt6_packages)
  set(_slicer_qt_cmake_dir "")
  set(_slicer_qt_prefix "")
  set(_slicer_qt_saved_prefix_path "${CMAKE_PREFIX_PATH}")
  if(Slicer_REQUIRED_QT_VERSION VERSION_GREATER_EQUAL "6" AND DEFINED Qt6_DIR AND EXISTS "${Qt6_DIR}"
      AND "${QT_HOST_PATH}" STREQUAL "")
    get_filename_component(_slicer_qt_cmake_dir "${Qt6_DIR}/.." ABSOLUTE)
    get_filename_component(_slicer_qt_prefix "${Qt6_DIR}/../../.." ABSOLUTE)

    get_cmake_property(_slicer_qt_cache_vars CACHE_VARIABLES)
    foreach(_var IN LISTS _slicer_qt_cache_vars)
      if(NOT _var MATCHES "^(Qt6[A-Za-z0-9]+)_DIR$" OR _var STREQUAL "Qt6_DIR")
        continue()
      endif()
      set(_pkg "${CMAKE_MATCH_1}")
      set(_value "${${_var}}")
      get_filename_component(_value_name "${_value}" NAME)
      if(NOT _value_name STREQUAL _pkg)
        continue() # not a Qt package directory (or <Package>_DIR-NOTFOUND)
      endif()
      get_filename_component(_value_parent "${_value}/.." ABSOLUTE)
      if(NOT _value_parent STREQUAL _slicer_qt_cmake_dir)
        message(STATUS "Forgetting ${_var}=${_value}: it is not part of the Qt in ${_slicer_qt_cmake_dir}")
        unset(${_var} CACHE)
        unset(${_var})
      endif()
    endforeach()

    # Search the selected Qt first (CMAKE_PREFIX_PATH comes before PATH and the
    # system prefixes). "<entry>/<Package>*/" is a search location of find_package,
    # so the CMake folder of Qt is enough; the whole prefix is not added, which
    # would also put its include and lib folders (for example all of Homebrew's)
    # first for find_path and find_library.
    list(PREPEND CMAKE_PREFIX_PATH "${_slicer_qt_cmake_dir}")
  endif()
endmacro()

#-----------------------------------------------------------------------------
# slicer_restore_qt6_prefix_path()
#
# Restore the CMAKE_PREFIX_PATH saved by slicer_isolate_qt6_packages().
macro(slicer_restore_qt6_prefix_path)
  if(NOT "${_slicer_qt_cmake_dir}" STREQUAL "")
    set(CMAKE_PREFIX_PATH "${_slicer_qt_saved_prefix_path}")
  endif()
endmacro()

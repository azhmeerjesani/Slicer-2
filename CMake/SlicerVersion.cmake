
#
# This module will set the variables Slicer_VERSION and Slicer_VERSION_FULL.
#
# It will also set all variables describing the SCM associated
# with Slicer_MAIN_PROJECT_SOURCE_DIR.
#
# It has been designed to be included in the build system of Slicer.
#
# The following variables are expected to be defined in the including scope:
#  GIT_EXECUTABLE
#  Slicer_CMAKE_DIR
#  Slicer_MAIN_PROJECT_APPLICATION_NAME
#  Slicer_MAIN_PROJECT_SOURCE_DIR
#  Slicer_RELEASE_TYPE
#  Slicer_VERSION_MAJOR
#  Slicer_VERSION_MINOR
#  Slicer_VERSION_PATCH
#

# --------------------------------------------------------------------------
# Sanity checks
# --------------------------------------------------------------------------
set(expected_defined_vars
  GIT_EXECUTABLE
  Slicer_CMAKE_DIR
  Slicer_MAIN_PROJECT_APPLICATION_NAME
  Slicer_MAIN_PROJECT_SOURCE_DIR
  Slicer_RELEASE_TYPE
  Slicer_VERSION_MAJOR
  Slicer_VERSION_MINOR
  Slicer_VERSION_PATCH
  )
foreach(var ${expected_defined_vars})
  if(NOT DEFINED ${var})
    message(FATAL_ERROR "${var} is mandatory")
  endif()
endforeach()

message(STATUS "Configuring ${Slicer_MAIN_PROJECT_APPLICATION_NAME} release type [${Slicer_RELEASE_TYPE}]")

#-----------------------------------------------------------------------------
# Update CMake module path
#-----------------------------------------------------------------------------
set(CMAKE_MODULE_PATH
  ${Slicer_CMAKE_DIR}
  ${CMAKE_MODULE_PATH}
  )

include(SlicerMacroExtractRepositoryInfo)

#-----------------------------------------------------------------------------
# Slicer version number
#-----------------------------------------------------------------------------
SlicerMacroExtractRepositoryInfo(
  VAR_PREFIX Slicer
  SOURCE_DIR ${Slicer_SOURCE_DIR}
  )

if(NOT "${Slicer_FORCED_WC_LAST_CHANGED_DATE}" STREQUAL "")
  set(Slicer_WC_LAST_CHANGED_DATE "${Slicer_FORCED_WC_LAST_CHANGED_DATE}")
endif()
string(REGEX REPLACE ".*([0-9][0-9][0-9][0-9]\\-[0-9][0-9]\\-[0-9][0-9]).*" "\\1"
  Slicer_BUILDDATE "${Slicer_WC_LAST_CHANGED_DATE}")

# Set Slicer_COMMIT_COUNT from working copy commit count adjusted by a custom offset.
if("${Slicer_WC_COMMIT_COUNT_OFFSET}" STREQUAL "")
  set(Slicer_WC_COMMIT_COUNT_OFFSET "0")
endif()
math(EXPR Slicer_COMMIT_COUNT "${Slicer_WC_COMMIT_COUNT}+${Slicer_WC_COMMIT_COUNT_OFFSET}")

if("${Slicer_REVISION_TYPE}" STREQUAL "")
  set(Slicer_REVISION_TYPE "CommitCount")
endif()

if(NOT "${Slicer_FORCED_REVISION}" STREQUAL "")
  set(Slicer_REVISION "${Slicer_FORCED_REVISION}")
elseif(Slicer_REVISION_TYPE STREQUAL "CommitCount")
  set(Slicer_REVISION "${Slicer_COMMIT_COUNT}")
elseif(Slicer_REVISION_TYPE STREQUAL "Hash")
  set(Slicer_REVISION "${Slicer_WC_REVISION_HASH}")
else()
  message(FATAL_ERROR "Invalid Slicer_REVISION_TYPE value: ${Slicer_REVISION_TYPE}")
endif()

# Slicer Studio fork: the revision the extensions server knows this build by. The server
# (slicer-packages.kitware.com, extensions.slicer.org) matches the revision exactly and has extensions
# only for official builds, so the fork's own Slicer_REVISION (the commit count of the clone it was
# built from) matches none and the Extensions Manager offered no extension. The fork is based on
# upstream commit bccfb40, the source of the official 5.13.0-2026-10-16 build (revision 35389), so the
# extensions built for that build are the ones built for this code. Update Slicer_UPSTREAM_REVISION
# when a newer upstream is merged. Slicer_REVISION itself is unchanged: it names the settings file and
# the extensions folder of this build. A forced revision is used as it is, as in official Slicer.
set(Slicer_UPSTREAM_REVISION "35389")
if(NOT "${Slicer_FORCED_REVISION}" STREQUAL "")
  set(Slicer_EXTENSIONS_SERVER_REVISION "${Slicer_REVISION}")
else()
  set(Slicer_EXTENSIONS_SERVER_REVISION "${Slicer_UPSTREAM_REVISION}")
endif()

set(Slicer_VERSION      "${Slicer_VERSION_MAJOR}.${Slicer_VERSION_MINOR}")
set(Slicer_VERSION_FULL "${Slicer_VERSION}.${Slicer_VERSION_PATCH}")

if(NOT "${Slicer_RELEASE_TYPE}" STREQUAL "Stable")
  set(Slicer_VERSION_FULL "${Slicer_VERSION_FULL}-${Slicer_BUILDDATE}")
endif()

message(STATUS "Configuring Slicer version [${Slicer_VERSION_FULL}]")
message(STATUS "Configuring Slicer revision [${Slicer_REVISION}]")
message(STATUS "Configuring Slicer extensions server revision [${Slicer_EXTENSIONS_SERVER_REVISION}]")

#-----------------------------------------------------------------------------
# Slicer main application version number
#-----------------------------------------------------------------------------
SlicerMacroExtractRepositoryInfo(
  VAR_PREFIX Slicer_MAIN_PROJECT
  SOURCE_DIR ${Slicer_MAIN_PROJECT_SOURCE_DIR}
  )

if(NOT "${Slicer_MAIN_PROJECT_FORCED_WC_LAST_CHANGED_DATE}" STREQUAL "")
  set(Slicer_MAIN_PROJECT_WC_LAST_CHANGED_DATE "${Slicer_MAIN_PROJECT_FORCED_WC_LAST_CHANGED_DATE}")
endif()
string(REGEX REPLACE ".*([0-9][0-9][0-9][0-9]\\-[0-9][0-9]\\-[0-9][0-9]).*" "\\1"
  Slicer_MAIN_PROJECT_BUILDDATE "${Slicer_MAIN_PROJECT_WC_LAST_CHANGED_DATE}")

# Set Slicer_MAIN_PROJECT_COMMIT_COUNT from working copy commit count adjusted by a custom offset.
if("${Slicer_MAIN_PROJECT_WC_COMMIT_COUNT_OFFSET}" STREQUAL "")
  if ("${Slicer_MAIN_PROJECT_APPLICATION_NAME}" STREQUAL "Slicer")
    # Force SlicerApp's commit count offset to be the same as Slicer_WC_COMMIT_COUNT_OFFSET to make
    # Slicer_MAIN_PROJECT_REVISION the same as Slicer_REVISION if the default Slicer application is built.
    set(Slicer_MAIN_PROJECT_WC_COMMIT_COUNT_OFFSET "${Slicer_WC_COMMIT_COUNT_OFFSET}")
  else()
    set(Slicer_MAIN_PROJECT_WC_COMMIT_COUNT_OFFSET "0")
  endif()
endif()
math(EXPR Slicer_MAIN_PROJECT_COMMIT_COUNT "${Slicer_MAIN_PROJECT_WC_COMMIT_COUNT}+${Slicer_MAIN_PROJECT_WC_COMMIT_COUNT_OFFSET}")

if("${Slicer_MAIN_PROJECT_REVISION_TYPE}" STREQUAL "")
  if ("${Slicer_MAIN_PROJECT_APPLICATION_NAME}" STREQUAL "Slicer")
    # Force SlicerApp's revision type to be the same as Slicer_REVISION_TYPE to make
    # Slicer_MAIN_PROJECT_REVISION the same as Slicer_REVISION if the default Slicer application is built.
    set(Slicer_MAIN_PROJECT_REVISION_TYPE "${Slicer_REVISION_TYPE}")
  else()
    set(Slicer_MAIN_PROJECT_REVISION_TYPE "CommitCount")
  endif()
endif()

if(NOT "${Slicer_MAIN_PROJECT_FORCED_REVISION}" STREQUAL "")
  set(Slicer_MAIN_PROJECT_REVISION "${Slicer_FORCED_REVISION}")
elseif(Slicer_MAIN_PROJECT_REVISION_TYPE STREQUAL "CommitCount")
  set(Slicer_MAIN_PROJECT_REVISION "${Slicer_MAIN_PROJECT_COMMIT_COUNT}")
elseif(Slicer_MAIN_PROJECT_REVISION_TYPE STREQUAL "Hash")
  set(Slicer_MAIN_PROJECT_REVISION "${Slicer_MAIN_PROJECT_WC_REVISION_HASH}")
else()
  message(FATAL_ERROR "Invalid Slicer_MAIN_PROJECT_REVISION_TYPE value: ${Slicer_MAIN_PROJECT_REVISION_TYPE}")
endif()

set(Slicer_MAIN_PROJECT_VERSION      "${Slicer_MAIN_PROJECT_VERSION_MAJOR}.${Slicer_MAIN_PROJECT_VERSION_MINOR}")
set(Slicer_MAIN_PROJECT_VERSION_FULL "${Slicer_MAIN_PROJECT_VERSION}.${Slicer_MAIN_PROJECT_VERSION_PATCH}")

if(NOT "${Slicer_RELEASE_TYPE}" STREQUAL "Stable")
  set(Slicer_MAIN_PROJECT_VERSION_FULL "${Slicer_MAIN_PROJECT_VERSION_FULL}-${Slicer_MAIN_PROJECT_BUILDDATE}")
endif()

if(NOT "${Slicer_MAIN_PROJECT_APPLICATION_NAME}" STREQUAL "Slicer")
  message(STATUS "Configuring ${Slicer_MAIN_PROJECT_APPLICATION_NAME} version [${Slicer_MAIN_PROJECT_VERSION_FULL}]")
  message(STATUS "Configuring ${Slicer_MAIN_PROJECT_APPLICATION_NAME} revision [${Slicer_MAIN_PROJECT_REVISION}]")
endif()

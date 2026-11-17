################################################################################
#
#  Slicer Studio: build information for the code updates from main
#
#  An installed Slicer Studio takes the Python and resource files of new commits
#  on main between releases (Modules/Scripted/Studio/StudioLib/updates_commit.py).
#  It may only replace files that the build copies verbatim, so the build records
#  them here:
#
#  - slicerStudioRecordVerbatimFiles() is called next to the install rules that
#    copy Python files and resources verbatim (slicerMacroBuildScriptedModule,
#    Base/Python, the lesion network mapping library). It repeats the path rules
#    of ctkMacroCompilePythonScript: ".py" is appended to a script without it, a
#    relative path keeps its sub-folders, the install folder gets the name of the
#    destination folder unless NO_INSTALL_SUBDIR is given, and the install root
#    (Slicer_INSTALL_ROOT: "./" on Linux, "<App>.app/Contents/" on macOS) is
#    removed, so that installed paths are relative to the Slicer home on every
#    platform. Generated files (absolute paths, files that are not in the source
#    tree) are not recorded: a file that is not recorded is never updated from a
#    commit (a change to it waits for a new build).
#  - at the end of the configuration the records are written to
#    ${Slicer_BINARY_DIR}/SlicerStudioVerbatimFiles.tsv (only when they changed);
#  - the SlicerStudioBuildInfo target runs at every build (it has no output, so a
#    new commit is picked up without a new configuration) and writes
#    bin/Python/studio_build_info.json and bin/Python/studio_build_files.json.gz
#    (Utilities/Scripts/SlicerStudioRelease/make_build_info.py), which are
#    installed with the application.
#
#  Calls of ctkMacroCompilePythonScript that are not recorded (for example the
#  Python plug-ins of loadable modules) are safe: their files are "unmapped" and
#  a change to them is reported as needing a new build.
#
################################################################################

set_property(GLOBAL PROPERTY _SLICER_STUDIO_VERBATIM_FILES "")

#-----------------------------------------------------------------------------
# The path of an install folder relative to the Slicer home
function(_slicer_studio_home_relative var path)
  set(_path "${path}")
  string(LENGTH "${Slicer_INSTALL_ROOT}" _length)
  if(_length GREATER 0)
    string(FIND "${_path}" "${Slicer_INSTALL_ROOT}" _position)
    if(_position EQUAL 0)
      string(SUBSTRING "${_path}" ${_length} -1 _path)
    endif()
  endif()
  string(REGEX REPLACE "^(\\./)+" "" _path "${_path}")
  string(REGEX REPLACE "//+" "/" _path "${_path}")
  string(REGEX REPLACE "/+$" "" _path "${_path}")
  set(${var} "${_path}" PARENT_SCOPE)
endfunction()

#-----------------------------------------------------------------------------
# slicerStudioRecordVerbatimFiles(
#   SCRIPTS <files> RESOURCES <files> [SOURCE_DIR <dir>]
#   DESTINATION_DIR <build dir> INSTALL_DIR <install dir> [NO_INSTALL_SUBDIR]
#   [MODULE_NAME <scripted module>] [PYTHON_ROOT <install dir on sys.path>])
#
# Same arguments as ctkMacroCompilePythonScript, plus MODULE_NAME (the file
# <MODULE_NAME>.py is the scripted module) and PYTHON_ROOT (the installed folder
# that is on sys.path when it is not the install folder itself, for example the
# scripted modules folder of a module installed in a sub-folder).
function(slicerStudioRecordVerbatimFiles)
  set(options NO_INSTALL_SUBDIR)
  set(oneValueArgs SOURCE_DIR DESTINATION_DIR INSTALL_DIR MODULE_NAME PYTHON_ROOT)
  set(multiValueArgs SCRIPTS RESOURCES)
  cmake_parse_arguments(MY "${options}" "${oneValueArgs}" "${multiValueArgs}" ${ARGN})
  if(NOT MY_DESTINATION_DIR OR NOT MY_INSTALL_DIR)
    message(FATAL_ERROR "slicerStudioRecordVerbatimFiles: DESTINATION_DIR and INSTALL_DIR are mandatory")
  endif()
  if(NOT MY_SOURCE_DIR)
    set(MY_SOURCE_DIR ${CMAKE_CURRENT_SOURCE_DIR})
  endif()
  get_filename_component(_source_dir "${MY_SOURCE_DIR}" REALPATH)
  get_filename_component(_destination_dir "${MY_DESTINATION_DIR}" REALPATH)
  get_filename_component(_slicer_source_dir "${Slicer_SOURCE_DIR}" REALPATH)
  get_filename_component(_slicer_binary_dir "${Slicer_BINARY_DIR}" REALPATH)

  # Installed folder of the files (as ctkMacroCompilePythonScript installs the destination folder)
  set(_install_base "${MY_INSTALL_DIR}")
  if(NOT MY_NO_INSTALL_SUBDIR)
    get_filename_component(_subdir "${_destination_dir}" NAME)
    set(_install_base "${_install_base}/${_subdir}")
  endif()
  _slicer_studio_home_relative(_install_base "${_install_base}")
  if(MY_PYTHON_ROOT)
    _slicer_studio_home_relative(_root "${MY_PYTHON_ROOT}")
  else()
    set(_root "${_install_base}")
  endif()
  set(_below "")
  if(NOT _install_base STREQUAL _root)
    string(LENGTH "${_root}/" _length)
    string(LENGTH "${_install_base}" _base_length)
    if(_base_length LESS_EQUAL _length)
      message(WARNING "slicerStudioRecordVerbatimFiles: ${_install_base} is not inside ${_root}: not recorded")
      return()
    endif()
    string(SUBSTRING "${_install_base}" 0 ${_length} _start)
    if(NOT _start STREQUAL "${_root}/")
      message(WARNING "slicerStudioRecordVerbatimFiles: ${_install_base} is not inside ${_root}: not recorded")
      return()
    endif()
    string(SUBSTRING "${_install_base}" ${_length} -1 _below)
    set(_below "${_below}/")
  endif()

  set(_records "")
  foreach(_kind SCRIPTS RESOURCES)
    foreach(_file IN LISTS MY_${_kind})
      if(_kind STREQUAL "SCRIPTS")
        get_filename_component(_extension "${_file}" EXT)
        if(NOT "${_extension}" MATCHES "py")
          set(_file "${_file}.py")
        endif()
      endif()
      if(IS_ABSOLUTE "${_file}" OR _file MATCHES "(^|/)\\.\\.(/|$)")
        continue()  # generated in the build tree, or outside the module folder
      endif()
      set(_source "${_source_dir}/${_file}")
      if(NOT EXISTS "${_source}" OR IS_DIRECTORY "${_source}")
        continue()  # generated
      endif()
      string(FIND "${_source}/" "${_slicer_binary_dir}/" _in_binary_dir)
      if(_in_binary_dir EQUAL 0)
        continue()
      endif()
      file(RELATIVE_PATH _source_relative "${_slicer_source_dir}" "${_source}")
      if(_source_relative MATCHES "^\\.\\./" OR IS_ABSOLUTE "${_source_relative}")
        continue()
      endif()
      set(_module "")
      if(MY_MODULE_NAME AND _file STREQUAL "${MY_MODULE_NAME}.py")
        set(_module "${MY_MODULE_NAME}")
      endif()
      string(APPEND _records "${_source_relative}\t${_root}\t${_below}${_file}\t${_module}\n")
    endforeach()
  endforeach()
  set_property(GLOBAL APPEND_STRING PROPERTY _SLICER_STUDIO_VERBATIM_FILES "${_records}")
endfunction()

#-----------------------------------------------------------------------------
# At the end of the configuration: the records, and the target that writes the build information
function(_slicer_studio_finish_build_info)
  set(_records_file "${Slicer_BINARY_DIR}/SlicerStudioVerbatimFiles.tsv")
  get_property(_records GLOBAL PROPERTY _SLICER_STUDIO_VERBATIM_FILES)
  file(WRITE "${_records_file}.tmp" "${_records}")
  # Changed only when the content changed (no rebuild of what depends on it otherwise)
  execute_process(COMMAND ${CMAKE_COMMAND} -E copy_if_different "${_records_file}.tmp" "${_records_file}")
  file(REMOVE "${_records_file}.tmp")

  set(_out "${Slicer_BINARY_DIR}/bin/Python")
  add_custom_target(SlicerStudioBuildInfo ALL
    COMMAND ${PYTHON_EXECUTABLE}
      ${Slicer_SOURCE_DIR}/Utilities/Scripts/SlicerStudioRelease/make_build_info.py
      --source ${Slicer_SOURCE_DIR}
      --records ${_records_file}
      --out ${_out}
      --scripted-root ${Slicer_QTSCRIPTEDMODULES_LIB_DIR}
    COMMENT "Recording the commit of this build (Slicer Studio code updates from main)"
    VERBATIM
    )
  # ${PYTHON_EXECUTABLE} is a launcher whose settings are written after the application links
  # (see SlicerGenerateLazyVtkModule in the top-level CMakeLists.txt).
  if(TARGET ${Slicer_MAIN_PROJECT_APPLICATION_NAME}ConfigureLauncher)
    add_dependencies(SlicerStudioBuildInfo ${Slicer_MAIN_PROJECT_APPLICATION_NAME}ConfigureLauncher)
  endif()
endfunction()
cmake_language(DEFER DIRECTORY ${Slicer_SOURCE_DIR} CALL _slicer_studio_finish_build_info)

install(FILES
  ${Slicer_BINARY_DIR}/bin/Python/studio_build_info.json
  ${Slicer_BINARY_DIR}/bin/Python/studio_build_files.json.gz
  DESTINATION ${Slicer_INSTALL_BIN_DIR}/Python
  COMPONENT Runtime
  )

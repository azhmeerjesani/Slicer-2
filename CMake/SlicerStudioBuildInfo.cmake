################################################################################
#
#  Slicer Studio: build information for the code updates from main
#
#  An installed Slicer Studio takes the Python and resource files of new commits
#  on main between releases (Modules/Scripted/Studio/StudioLib/updates_commit.py).
#  It may only replace files that the build copies verbatim, so the build records
#  them here:
#
#  - slicerStudioRecordVerbatimFiles() (SlicerStudioRecordVerbatimFiles.cmake) is called next to the install rules that
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

include(${CMAKE_CURRENT_LIST_DIR}/SlicerStudioRecordVerbatimFiles.cmake)

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


if(Slicer_USE_CTKAPPLAUNCHER)

  set(proj CTKAPPLAUNCHER)

  # Sanity checks
  if(DEFINED CTKAPPLAUNCHER_DIR AND NOT EXISTS ${CTKAPPLAUNCHER_DIR})
    message(FATAL_ERROR "CTKAPPLAUNCHER_DIR variable is defined but corresponds to nonexistent directory")
  endif()

  # On macOS the launcher is built from source, natively and against the Qt of
  # the build: CTKAppLauncher is only released as an x86_64 macOS binary, which
  # needs Rosetta 2 (that macOS 28 no longer supports), and it is shipped in
  # the application as bin/PythonSlicer and bin/SlicerDesigner. The
  # CTKAppLauncherLib project already builds the whole AppLauncher project, and
  # its build tree (bin/CTKAppLauncher, CTKAppLauncherConfig.cmake) is a valid
  # CTKAppLauncher_DIR.
  if(APPLE)
    option(Slicer_CTKAPPLAUNCHER_FROM_SOURCE "Build the CTK application launcher from source (macOS)" ON)
    mark_as_advanced(Slicer_CTKAPPLAUNCHER_FROM_SOURCE)
  else()
    set(Slicer_CTKAPPLAUNCHER_FROM_SOURCE OFF)
  endif()

  # Set dependency list
  set(${proj}_DEPENDENCIES "")
  if(WIN32)
    set(${proj}_DEPENDENCIES CTKResEdit)
  endif()
  if(Slicer_CTKAPPLAUNCHER_FROM_SOURCE)
    set(${proj}_DEPENDENCIES CTKAppLauncherLib)
  endif()

  # Include dependent projects if any
  ExternalProject_Include_Dependencies(${proj} PROJECT_VAR proj DEPENDS_VAR ${proj}_DEPENDENCIES)

  if(Slicer_USE_SYSTEM_${proj})
    message(FATAL_ERROR "Enabling Slicer_USE_SYSTEM_${proj} is not supported !")
  endif()

  if(NOT DEFINED CTKAppLauncher_DIR AND Slicer_CTKAPPLAUNCHER_FROM_SOURCE)
    set(CTKAppLauncher_DIR ${CTKAppLauncherLib_DIR})
    ExternalProject_Add_Empty(${proj} DEPENDS ${${proj}_DEPENDENCIES})

  elseif(NOT DEFINED CTKAppLauncher_DIR)

    SlicerMacroGetOperatingSystemArchitectureBitness(VAR_PREFIX CTKAPPLAUNCHER)
    set(launcher_version "0.1.34")
    if("${CTKAPPLAUNCHER_OS}" STREQUAL "win")
      # CTKAppLauncher is only released as an i386 Windows binary, so use it unconditionally.
      set(CTKAPPLAUNCHER_ARCHITECTURE "i386")
      set(sha256 "8b91093a18476749b5687dc433909d61916a9983e832be3fb0a43d494b208924")
    elseif("${CTKAPPLAUNCHER_OS}" STREQUAL "linux")
      set(sha256 "e39f82151be485e2e097a254077c4f3b7c962765c0e1564cbc09360355f87b76")
    elseif("${CTKAPPLAUNCHER_OS}" STREQUAL "macosx")
      # Only with Slicer_CTKAPPLAUNCHER_FROM_SOURCE=OFF: the released macOS
      # binary is amd64 only (it runs on arm64 Macs through Rosetta 2).
      set(CTKAPPLAUNCHER_ARCHITECTURE "amd64")
      set(sha256 "9d2f08fd68071562b17a2bfc18b6510a2a19596c593bf19deda1a9f1d3038ad0")
    endif()

    set(EP_BINARY_DIR ${CMAKE_BINARY_DIR}/${proj})

    set(CTKAppLauncherFileName CTKAppLauncher-${launcher_version}-${CTKAPPLAUNCHER_OS}-${CTKAPPLAUNCHER_ARCHITECTURE}.tar.gz)
    ExternalProject_Add(${proj}
      ${${proj}_EP_ARGS}
      URL https://github.com/commontk/AppLauncher/releases/download/v${launcher_version}/${CTKAppLauncherFileName}
      URL_HASH SHA256=${sha256}
      DOWNLOAD_DIR ${CMAKE_BINARY_DIR}
      SOURCE_DIR ${EP_BINARY_DIR}
      BUILD_IN_SOURCE 1
      CONFIGURE_COMMAND ""
      BUILD_COMMAND ""
      INSTALL_COMMAND ""
      DEPENDS
        ${${proj}_DEPENDENCIES}
      )

    ExternalProject_GenerateProjectDescription_Step(${proj}
      VERSION ${launcher_version}
      LICENSE_FILES "https://raw.githubusercontent.com/commontk/AppLauncher/v${launcher_version}/LICENSE_Apache_20"
      )

    set(CTKAppLauncher_DIR ${EP_BINARY_DIR})

  else()
    ExternalProject_Add_Empty(${proj} DEPENDS ${${proj}_DEPENDENCIES})
  endif()

  mark_as_superbuild(
    VARS
      CTKAppLauncher_DIR:PATH
    LABELS "FIND_PACKAGE"
    )

endif()

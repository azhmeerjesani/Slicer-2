
project(SlicerApp)

include(SlicerMacroBuildApplication)

# --------------------------------------------------------------------------
# Properties
# --------------------------------------------------------------------------
SlicerReadApplicationProperties()

# --------------------------------------------------------------------------
# Folder
# --------------------------------------------------------------------------
set(${PROJECT_NAME}_FOLDER "App-${SlicerApp_APPLICATION_NAME}")

# --------------------------------------------------------------------------
# Application library
# --------------------------------------------------------------------------
set(APPLIB_NAME "q${PROJECT_NAME}")

set(APPLIB_SRCS
  qSlicerAppMainWindow.cxx
  qSlicerAppMainWindow.h
  qSlicerAppMainWindow_p.h
  )

set(APPLIB_UI_SRCS
  )

set(APPLIB_RESOURCES
  Resources/SlicerApp.qrc
  ${qSlicerBaseQTGUI_SOURCE_DIR}/Resources/qSlicerBaseQTGUI.qrc
  )

slicerMacroBuildAppLibrary(
  NAME ${APPLIB_NAME}
  APPLICATION_NAME ${SlicerApp_APPLICATION_NAME}
  DESCRIPTION_SUMMARY ${SlicerApp_DESCRIPTION_SUMMARY}
  DESCRIPTION_FILE ${SlicerApp_DESCRIPTION_FILE}
  EXPORT_DIRECTIVE "Q_SLICER_APP_EXPORT"
  FOLDER ${${PROJECT_NAME}_FOLDER}
  SRCS ${APPLIB_SRCS}
  UI_SRCS ${APPLIB_UI_SRCS}
  RESOURCES ${APPLIB_RESOURCES}
  )

# --------------------------------------------------------------------------
# Application executable
# --------------------------------------------------------------------------

# Configure launcher only for the main application
set(extra_args)
if(${PROJECT_NAME} STREQUAL ${Slicer_MAIN_PROJECT})
  set(extra_args CONFIGURE_LAUNCHER)
endif()

set(APP_SRCS
  Main.cxx
  )

# Fork defaults: the macro copies Resources/SlicerDefaultSettings.ini to "<build>/${Slicer_SHARE_DIR}" and
# installs it into ${Slicer_INSTALL_SHARE_DIR}, which is where qSlicerCoreApplication::slicerDefaultSettingsFilePath()
# looks. The defaults are merged into the user settings after the command line is parsed, so --testing and
# --disable-settings keep using temporary settings.
slicerMacroBuildApplication(
  NAME ${PROJECT_NAME}
  APPLICATION_NAME ${${PROJECT_NAME}_APPLICATION_NAME}
  APPLICATION_DISPLAY_NAME ${${PROJECT_NAME}_APPLICATION_DISPLAY_NAME}
  LAUNCHER_SPLASHSCREEN_FILE ${SlicerApp_LAUNCHER_SPLASHSCREEN_FILE}
  APPLE_ICON_FILE ${SlicerApp_APPLE_ICON_FILE}
  WIN_ICON_FILE ${SlicerApp_WIN_ICON_FILE}
  LINUX_ICON_FILE ${SlicerApp_LINUX_ICON_FILE}
  LICENSE_FILE ${SlicerApp_LICENSE_FILE}
  DEFAULT_SETTINGS_FILE ${CMAKE_CURRENT_SOURCE_DIR}/Resources/SlicerDefaultSettings.ini
  FOLDER ${${PROJECT_NAME}_FOLDER}
  SRCS ${APP_SRCS}
  TARGET_LIBRARIES ${APPLIB_NAME}
  TARGET_NAME_VAR "APP_TARGET_NAME"
  ${extra_args}
  )

# --------------------------------------------------------------------------
# Linux desktop entry of Slicer Studio: the icons and the entry template go into
# share/Slicer-<version>/desktop, and install-desktop-entry.sh (which adds the
# entry to the user's applications menu) next to the launcher.
# --------------------------------------------------------------------------
if(UNIX AND NOT APPLE AND ${PROJECT_NAME} STREQUAL ${Slicer_MAIN_PROJECT})
  install(
    DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}/Resources/Linux/icons
    DESTINATION ${Slicer_INSTALL_SHARE_DIR}/desktop
    COMPONENT Runtime
    )
  install(
    FILES ${CMAKE_CURRENT_SOURCE_DIR}/Resources/Linux/slicer-studio.desktop.in
    DESTINATION ${Slicer_INSTALL_SHARE_DIR}/desktop
    COMPONENT Runtime
    )
  install(
    PROGRAMS ${CMAKE_CURRENT_SOURCE_DIR}/Resources/Linux/install-desktop-entry.sh
    DESTINATION "."
    COMPONENT Runtime
    )
endif()

# --------------------------------------------------------------------------
if(BUILD_TESTING)
  add_subdirectory(Testing)
endif()

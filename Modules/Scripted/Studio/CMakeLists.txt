#-----------------------------------------------------------------------------
set(MODULE_NAME Studio)

#-----------------------------------------------------------------------------
# The StudioLib package and the icons are globbed so that pages and plug-ins added
# later (for example StudioLib/library.py) are picked up without editing this file.
file(GLOB MODULE_PYTHON_LIB_SCRIPTS CONFIGURE_DEPENDS
  RELATIVE ${CMAKE_CURRENT_SOURCE_DIR}
  ${MODULE_NAME}Lib/*.py
  )

set(MODULE_PYTHON_SCRIPTS
  ${MODULE_NAME}.py
  ${MODULE_PYTHON_LIB_SCRIPTS}
  )

file(GLOB MODULE_PYTHON_RESOURCES CONFIGURE_DEPENDS
  RELATIVE ${CMAKE_CURRENT_SOURCE_DIR}
  Resources/Icons/*.svg
  Resources/Icons/Lucide/*
  )

#-----------------------------------------------------------------------------
slicerMacroBuildScriptedModule(
  NAME ${MODULE_NAME}
  SCRIPTS ${MODULE_PYTHON_SCRIPTS}
  RESOURCES ${MODULE_PYTHON_RESOURCES}
  WITH_GENERIC_TESTS
  )

#-----------------------------------------------------------------------------
if(BUILD_TESTING)
  # Additional build-time testing
  add_subdirectory(Testing)
endif()

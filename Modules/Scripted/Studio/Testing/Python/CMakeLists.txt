# The Studio shell needs the main window, therefore the test does not use --no-main-window
slicer_add_python_unittest(SCRIPT StudioTest.py)
slicer_add_python_unittest(SCRIPT StudioNavigationTest.py)
slicer_add_python_unittest(SCRIPT StudioOrientationTest.py)

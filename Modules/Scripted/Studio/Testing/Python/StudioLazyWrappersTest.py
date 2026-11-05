"""Tests of the lazy import of the loadable-module Python wrappers (setting Studio/LazyPythonWrappers, W4-03:
slicer.util._registerLazyWrapperIndex, slicer.__getattr__).

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioLazyWrappersTest'); slicer.app.exit(0)"

The tests register the index of a synthetic wrapper directory (a Python module named like a module logic wrapper,
with one VTK class), so that a class is pending whatever the state of the real wrappers of this build.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

import slicer

_MODULE = "vtkSlicerStudioStarProbeModuleLogicPython"
_CLASS = "vtkSlicerStudioStarProbeLogic"


class StudioLazyWrappersTest(unittest.TestCase):
    def setUp(self):
        try:
            import vtkmodules

            self.classModules = vtkmodules._CLASS_MODULES
        except (ImportError, AttributeError):
            self.skipTest("this VTK cannot import wrappers on demand: the wrappers are imported eagerly")
        if not isinstance(self.classModules, dict):
            self.skipTest("this VTK cannot import wrappers on demand: the wrappers are imported eagerly")
        self.directory = tempfile.mkdtemp(prefix="StudioLazyWrappersTest-")
        with open(os.path.join(self.directory, _MODULE + ".py"), "w", encoding="utf-8") as stream:
            stream.write(f"import vtk\n\n\nclass {_CLASS}(vtk.vtkObject):\n    pass\n")
        self.indexPath = os.path.join(self.directory, "SlicerWrapperIndex.json")
        with open(self.indexPath, "w", encoding="utf-8") as stream:
            json.dump({"format": 1, "modules": [[_MODULE, "vtk"]], "classes": {_CLASS: _MODULE}}, stream)
        sys.path.insert(0, self.directory)

    def tearDown(self):
        state = slicer.util._lazyWrapperState
        if _MODULE in state["pending"]:
            state["pending"].remove(_MODULE)
        state["imported"].discard(_MODULE)
        state["kinds"].pop(_MODULE, None)
        state["classes"].pop(_CLASS, None)
        key = (os.path.normcase(os.path.abspath(self.directory)), os.path.normcase(os.path.abspath(self.indexPath)))
        if key in state["indexes"]:
            state["indexes"].remove(key)
        if state["timer"] is not None and not state["pending"]:
            state["timer"].stop()
            state["timer"] = None
        slicer.__dict__.pop(_CLASS, None)
        if self.classModules.get(_CLASS) == _MODULE:
            del self.classModules[_CLASS]
        sys.modules.pop(_MODULE, None)
        if self.directory in sys.path:
            sys.path.remove(self.directory)
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_star_import_gets_pending_wrapper_classes(self):
        """'from slicer import *' gets the classes of wrappers not imported yet (old-style ~/.slicerrc.py and
        extensions use the bare names, as with the eager import and official 3D Slicer).
        """
        self.assertTrue(slicer.util._registerLazyWrapperIndex(self.directory, self.indexPath))
        self.assertIn(_MODULE, slicer.util._lazyWrapperState["pending"])
        self.assertNotIn(_CLASS, slicer.__dict__, "the synthetic class must still be pending")

        namespace = {}
        exec("from slicer import *", namespace)  # noqa: S102

        self.assertTrue(_CLASS in namespace, f"the star import did not get {_CLASS}")
        self.assertIs(namespace[_CLASS], slicer.__dict__[_CLASS])
        self.assertNotIn(_MODULE, slicer.util._lazyWrapperState["pending"])
        # As with the eager import, slicer has no __all__: the star import copies every public name.
        self.assertFalse(hasattr(slicer, "__all__"))
        self.assertNotIn("_lazyWrapperState", namespace)

    def test_attribute_access_stays_lazy(self):
        """Looking up another name does not import the pending wrappers (the setting keeps startup fast)."""
        self.assertTrue(slicer.util._registerLazyWrapperIndex(self.directory, self.indexPath))
        self.assertFalse(hasattr(slicer, "__studioNoSuchDunder__"))
        self.assertIn(_MODULE, slicer.util._lazyWrapperState["pending"])
        self.assertNotIn(_CLASS, slicer.__dict__)
        self.assertEqual(getattr(slicer, _CLASS).__name__, _CLASS)
        self.assertNotIn(_MODULE, slicer.util._lazyWrapperState["pending"])

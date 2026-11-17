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
import threading
import time
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
        state["removed"].discard(_CLASS)
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

    def test_abstract_plugin_class_registered_under_private_is_public(self):
        """A class that PythonQt registered under PythonQt.private (the abstract subject hierarchy plugin base
        class, when a plugin object is wrapped before its wrapper module is imported) is still available as
        slicer.<Class>, as in official 3D Slicer. Other classes under PythonQt.private (internal or not
        wrapped) are never copied into slicer, neither at import nor by a missed lookup.
        """
        import PythonQt

        name = "qStudioTestPrivateRegisteredClass"
        other = "qStudioTestInternalPrivateClass"
        try:
            setattr(PythonQt.private, name, slicer.qMRMLWidget)
            setattr(PythonQt.private, other, slicer.qMRMLWidget)
        except (AttributeError, TypeError):
            self.skipTest("PythonQt.private cannot be extended in this build")
        original = slicer.util._LAZY_WRAPPER_PRIVATE_CLASSES
        slicer.util._LAZY_WRAPPER_PRIVATE_CLASSES = original + (name,)
        try:
            self.assertFalse(name in slicer.__dict__)
            self.assertFalse(hasattr(slicer, "qStudioTestNoSuchName"))
            self.assertFalse(other in slicer.__dict__)
            self.assertIs(getattr(slicer, name), slicer.qMRMLWidget)
            slicer.__dict__.pop(name, None)
            slicer.util._copyLazyWrapperPrivateClasses(slicer)
            self.assertIs(slicer.__dict__.get(name), slicer.qMRMLWidget)
            self.assertFalse(other in slicer.__dict__)
            self.assertFalse(hasattr(slicer, other))
            self.assertNotIn("PythonQtPrivate", slicer.__dict__)
            self.assertNotIn("QMainWindowLayout", slicer.__dict__)
        finally:
            slicer.util._LAZY_WRAPPER_PRIVATE_CLASSES = original
            for n in (name, other):
                slicer.__dict__.pop(n, None)
                try:
                    delattr(PythonQt.private, n)
                except (AttributeError, TypeError):
                    pass

    def test_attribute_access_stays_lazy(self):
        """Looking up another name does not import the pending wrappers (the setting keeps startup fast)."""
        self.assertTrue(slicer.util._registerLazyWrapperIndex(self.directory, self.indexPath))
        self.assertFalse(hasattr(slicer, "__studioNoSuchDunder__"))
        self.assertIn(_MODULE, slicer.util._lazyWrapperState["pending"])
        self.assertNotIn(_CLASS, slicer.__dict__)
        self.assertEqual(getattr(slicer, _CLASS).__name__, _CLASS)
        self.assertNotIn(_MODULE, slicer.util._lazyWrapperState["pending"])

    def test_worker_thread_waits_for_import_in_progress(self):
        """A Python thread that reads a class while another thread imports its wrapper gets the class (it used to
        get AttributeError: the module was marked imported before its classes were copied into slicer).
        """
        with open(os.path.join(self.directory, _MODULE + ".py"), "w", encoding="utf-8") as stream:
            stream.write(f"import time\n\nimport vtk\n\ntime.sleep(0.6)\n\n\nclass {_CLASS}(vtk.vtkObject):\n    pass\n")
        self.assertTrue(slicer.util._registerLazyWrapperIndex(self.directory, self.indexPath))
        self.assertNotIn(_CLASS, slicer.__dict__)

        result = {}

        def worker():
            time.sleep(0.2)  # the main thread is inside the import of the wrapper by now
            try:
                result["name"] = getattr(slicer, _CLASS).__name__
            except Exception as detail:
                result["error"] = repr(detail)

        thread = threading.Thread(target=worker)
        thread.start()
        self.assertEqual(getattr(slicer, _CLASS).__name__, _CLASS)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, {"name": _CLASS})

    def test_assignment_survives_and_del_removes_pending_class(self):
        """slicer.<Class> = x and del slicer.<Class> on a class that is not imported yet behave as without lazy
        wrappers: the later import (idle prefetch) does not overwrite the assignment, and del removes the class.
        """
        self.assertTrue(slicer.util._registerLazyWrapperIndex(self.directory, self.indexPath))
        self.assertNotIn(_CLASS, slicer.__dict__)
        shim = type("Shim", (), {})
        setattr(slicer, _CLASS, shim)
        slicer.util._importPendingLazyWrappers()
        self.assertNotIn(_MODULE, slicer.util._lazyWrapperState["pending"])
        self.assertIs(getattr(slicer, _CLASS), shim, "the import overwrote the assigned class")

        # A pending class that is deleted stays deleted, also after its wrapper is imported
        slicer.__dict__.pop(_CLASS, None)
        state = slicer.util._lazyWrapperState
        state["imported"].discard(_MODULE)
        sys.modules.pop(_MODULE, None)
        state["classes"].pop(_CLASS, None)
        state["removed"].discard(_CLASS)
        key = (os.path.normcase(os.path.abspath(self.directory)), os.path.normcase(os.path.abspath(self.indexPath)))
        state["indexes"].remove(key)
        self.assertTrue(slicer.util._registerLazyWrapperIndex(self.directory, self.indexPath))
        delattr(slicer, _CLASS)
        self.assertFalse(hasattr(slicer, _CLASS))
        slicer.util._importPendingLazyWrappers()
        self.assertFalse(hasattr(slicer, _CLASS), "the import brought back the deleted class")
        with self.assertRaises(AttributeError):
            delattr(slicer, _CLASS)

    def test_reload_of_util_keeps_pending_wrappers(self):
        """importlib.reload(slicer.util) keeps the lazy wrapper state and lock: a class that is not imported yet
        stays available, and the idle prefetch (which holds the state) can still finish.
        """
        import importlib

        self.assertTrue(slicer.util._registerLazyWrapperIndex(self.directory, self.indexPath))
        state = slicer.util._lazyWrapperState
        lock = slicer.util._lazyWrapperLock
        self.assertIn(_MODULE, state["pending"])
        importlib.reload(slicer.util)
        self.assertIs(slicer.util._lazyWrapperState, state, "the reload replaced the lazy wrapper state")
        self.assertIs(slicer.util._lazyWrapperLock, lock)
        self.assertIn(_CLASS, slicer.util._lazyWrapperClassNames())
        self.assertTrue(hasattr(slicer, _CLASS))
        self.assertNotIn(_MODULE, state["pending"])

    def test_index_generator_skips_object_factory_initialize_helpers(self):
        """The index lists only classes that slicer.<Class> can resolve: the "<Class>Initialize" helper types that
        VTK wraps for object factories (not vtkObjectBase subclasses) are left out, so dir(slicer) has no phantom.
        """
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), *([os.pardir] * 5), "Utilities", "Scripts", "GenerateSlicerWrapperIndex.py",
        )
        if not os.path.isfile(script):
            self.skipTest("the source tree is not available")
        import importlib.util

        spec = importlib.util.spec_from_file_location("GenerateSlicerWrapperIndexForTest", script)
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        data = b"\0vtkFooModulePython.vtkFooLogic\0vtkFooModulePython.vtkFooFactoryInitialize\0"
        self.assertEqual(generator.vtk_class_names("vtkFooModulePython", data), {"vtkFooLogic"})

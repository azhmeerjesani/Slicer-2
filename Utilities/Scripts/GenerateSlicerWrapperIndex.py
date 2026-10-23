"""Generate the class index of the loadable-module Python wrappers.

At startup, the first instantiated module imports every Python wrapper of the
loadable modules (``vtkSlicer*Module*Python`` and ``qSlicer*PythonQt``) found
in the qt-loadable-modules directory and copies their classes into the
``slicer`` namespace (``qSlicerScriptedUtils::importModulePythonExtensions``).
With the ``Studio/LazyPythonWrappers`` setting, the application instead reads
the index written by this script, which maps each class to the wrapper module
that provides it, and imports a wrapper only when one of its classes is first
needed (``slicer.<Class>``, or an object of the class returned from C++), then
imports the remaining wrappers while the application is idle.

The index is built without importing anything, so that it can run in the bare
build environment: the class names are read from the wrapper libraries.

- VTK wrappers: each wrapped class has a Python type named
  ``<module>.<class>``, stored as a string in the library.
- PythonQt wrappers: each wrapped class ``<class>`` has a decorator class
  ``PythonQtWrapper_<class>``, whose name is stored both in its meta-object
  string data and in its type information (``<length>PythonQtWrapper_<class>``).

The index can hold a few names that the eager import would not copy into
``slicer`` (wrapped types that are not ``vtkObjectBase`` subclasses), and it
cannot see the abstract Qt base classes that PythonQt registers implicitly.
The application checks each class when it imports its wrapper, and imports all
remaining wrappers when a class-like name is not in the index, so both cases
behave as with the eager import.

The output is JSON::

  {"format": 1,
   "modules": [["vtkSlicerColorsModuleLogicPython", "vtk"], ...],
   "classes": {"vtkSlicerColorLogic": "vtkSlicerColorsModuleLogicPython", ...}}

``modules`` lists the wrappers in the order of the eager import (by pattern,
then by name) and ``classes`` resolves duplicate class names to the last
module in that order, as the eager import does. The file holds module and
class names only, no paths. It is rewritten only when its content changes.

Usage: GenerateSlicerWrapperIndex.py --directory <qt-loadable-modules dir> --output <index.json>
"""

import argparse
import fnmatch
import json
import os
import re
import sys

# The patterns of qSlicerScriptedUtils::importModulePythonExtensions, in the same order.
WRAPPER_PATTERNS = (
    ("vtkSlicer*ModuleLogicPython.*", "vtk"),
    ("vtkSlicer*ModuleMRMLPython.*", "vtk"),
    ("vtkSlicer*ModuleMRMLDisplayableManagerPython.*", "vtk"),
    ("vtkSlicer*ModuleVTKWidgetsPython.*", "vtk"),
    ("qSlicer*PythonQt.*", "qt"),
)

# File name extensions of Python extension modules (Linux and macOS, Windows).
EXTENSION_SUFFIXES = (".so", ".pyd")

FORMAT_VERSION = 1

_IDENTIFIER = rb"[A-Za-z_][A-Za-z0-9_]*"
_QT_WRAPPER_PREFIX = b"PythonQtWrapper_"


def wrapper_modules(directory):
    """Return [(module_name, kind, path)] in the order of the eager import."""
    try:
        file_names = sorted(os.listdir(directory))
    except OSError:
        return []
    modules = []
    seen = set()
    for pattern, kind in WRAPPER_PATTERNS:
        for file_name in file_names:
            if not fnmatch.fnmatchcase(file_name, pattern):
                continue
            module_name, extension = os.path.splitext(file_name)
            if extension not in EXTENSION_SUFFIXES or module_name in seen:
                continue
            seen.add(module_name)
            modules.append((module_name, kind, os.path.join(directory, file_name)))
    return modules


def vtk_class_names(module_name, data):
    """Return the names of the Python types defined by a VTK wrapper library."""
    pattern = re.compile(rb"\0" + re.escape(module_name.encode()) + rb"\.(" + _IDENTIFIER + rb")\0")
    # The library file name ("<module>.so") matches the pattern too.
    return {name.decode() for name in pattern.findall(data)} - {suffix[1:] for suffix in EXTENSION_SUFFIXES}


def qt_class_names(data):
    """Return the names of the classes wrapped by a PythonQt wrapper library."""
    names = set()
    pattern = re.compile(rb"(\0|[0-9]+)" + _QT_WRAPPER_PREFIX + rb"(" + _IDENTIFIER + rb")(?=\0)")
    for prefix, name in pattern.findall(data):
        # A meta-object string ("\0PythonQtWrapper_<class>\0"), or a type information name whose
        # length prefix covers exactly the decorator class name. Mangled member names, which
        # continue after the class name, do not match either form.
        if prefix == b"\0" or int(prefix) == len(_QT_WRAPPER_PREFIX) + len(name):
            names.add(name.decode())
    return names


def build_index(directory):
    modules = []
    classes = {}
    for module_name, kind, path in wrapper_modules(directory):
        with open(path, "rb") as stream:
            data = stream.read()
        names = vtk_class_names(module_name, data) if kind == "vtk" else qt_class_names(data)
        modules.append([module_name, kind])
        for name in sorted(names):
            classes[name] = module_name  # last module wins, as with the eager import
    return {"format": FORMAT_VERSION, "modules": modules, "classes": dict(sorted(classes.items()))}


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--directory", required=True, help="directory that holds the wrapper libraries")
    parser.add_argument("--output", required=True, help="index file to write")
    arguments = parser.parse_args(argv)

    index = build_index(arguments.directory)
    content = json.dumps(index, indent=0) + "\n"
    try:
        with open(arguments.output, encoding="utf-8") as stream:
            unchanged = stream.read() == content
    except OSError:
        unchanged = False
    if not unchanged:
        os.makedirs(os.path.dirname(os.path.abspath(arguments.output)), exist_ok=True)
        with open(arguments.output, "w", encoding="utf-8") as stream:
            stream.write(content)
    print("%s %s: %d classes from %d wrapper modules"
          % ("unchanged" if unchanged else "generated", arguments.output, len(index["classes"]), len(index["modules"])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

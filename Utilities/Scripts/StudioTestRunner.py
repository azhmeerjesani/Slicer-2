#!/usr/bin/env python3
"""Run Slicer tests headless, with the verification recipes of the Slicer Studio SPEC (section 9.5).

Usage::

  python3 Utilities/Scripts/StudioTestRunner.py [options] unittest DIR NAME [NAME ...]
  python3 Utilities/Scripts/StudioTestRunner.py [options] selftest MODULE
  python3 Utilities/Scripts/StudioTestRunner.py [options] generic MODULE
  python3 Utilities/Scripts/StudioTestRunner.py [options] pyunit DIR TESTFILE

- ``unittest``: ``slicer.testing.runUnitTest([DIR], NAME)`` with ``--testing``, one Slicer process per NAME.
  DIR may also be a module name (for example ``Segmentations`` or ``Studio``), meaning
  ``Modules/<Scripted|Loadable|CLI>/<module>/Testing/Python`` in this source tree.
- ``selftest``: ``slicer.util.selectModule(MODULE)`` then ``slicer.selfTests[MODULE]()``, with ``--testing``.
- ``generic``: the generic module test, configured from ``Base/QTCore/Testing/Python/qSlicerModuleGenericTest.py.in``
  into ``--generic-dir``, run with ``--testing --no-main-window`` (as ctest does for WITH_GENERIC_TESTS modules).
- ``pyunit``: ``PythonSlicer -m unittest TESTFILE`` in DIR (pure Python tests, no application). DIR may
  also be a module name, meaning the module's source directory.

Every child runs under ``xvfb-run`` (unless ``--no-xvfb``). The exit code is the child's, or the first
non-zero one when several tests run.
"""

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_SLICER = "/opt/S-r/Slicer-build/Slicer"
DEFAULT_PYTHONSLICER = "/opt/S-r/python-install/bin/PythonSlicer"
GENERIC_TEST_TEMPLATE = os.path.join(SOURCE_DIR, "Base", "QTCore", "Testing", "Python", "qSlicerModuleGenericTest.py.in")


def splitNames(values):
    """Accept "A B", "A,B" and "{A, B}" (as written in the work package texts)."""
    return [name for name in re.split(r"[\s,{}]+", " ".join(values)) if name]


def resolveDirectory(directory, subdirectory=""):
    """A directory, or for a module name its source directory (joined with subdirectory)."""
    if os.path.isdir(directory):
        return os.path.abspath(directory)
    for moduleType in ("Scripted", "Loadable", "CLI"):
        candidate = os.path.join(SOURCE_DIR, "Modules", moduleType, directory, subdirectory)
        if os.path.isdir(candidate):
            return os.path.normpath(candidate)
    raise ValueError(f"'{directory}' is neither a directory nor a module with a {subdirectory or 'source'} directory")


def pythonSlicerPath(args):
    if args.pythonslicer:
        return args.pythonslicer
    candidates = [shutil.which("PythonSlicer"), DEFAULT_PYTHONSLICER,
                  os.path.join(os.path.dirname(os.path.abspath(args.slicer)), "bin", "PythonSlicer")]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise ValueError("PythonSlicer not found; use --pythonslicer")


def run(command, args, label, cwd=None):
    """Run command (under xvfb-run unless disabled) and return its exit code."""
    if not args.no_xvfb:
        command = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] + command
    environment = dict(os.environ)
    if args.home:
        os.makedirs(args.home, exist_ok=True)
        environment["HOME"] = args.home
    print("+ " + shlex.join(command), flush=True)
    try:
        exitCode = subprocess.run(command, cwd=cwd, env=environment, timeout=args.timeout, check=False).returncode
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT after {args.timeout} s", flush=True)
        exitCode = 124
    print(f"{'PASSED' if exitCode == 0 else 'FAILED'}: {label} (exit code {exitCode})", flush=True)
    return exitCode


def slicerCommand(args, pythonCode, extraArguments=()):
    return [args.slicer, "--no-splash", "--testing", *extraArguments, *shlex.split(args.slicer_args), "--python-code", pythonCode]


def runUnitTests(args):
    directory = resolveDirectory(args.directory, os.path.join("Testing", "Python"))
    exitCode = 0
    for name in splitNames(args.names):
        code = f"import slicer.testing; slicer.testing.runUnitTest([{directory!r}], {name!r}); slicer.app.exit(0)"
        result = run(slicerCommand(args, code), args, f"unittest {name}")
        exitCode = exitCode or result
    return exitCode


def runSelfTest(args):
    module = args.module
    code = f"import slicer; slicer.util.selectModule({module!r}); slicer.selfTests[{module!r}](); slicer.app.exit(0)"
    return run(slicerCommand(args, code), args, f"selftest {module}")


def runGenericTest(args):
    module = args.module
    with open(GENERIC_TEST_TEMPLATE) as f:
        content = f.read().replace("@MODULENAME_LC@", module.lower()).replace("@MODULENAME@", module)
    os.makedirs(args.generic_dir, exist_ok=True)
    testName = f"qSlicer{module}ModuleGenericTest"
    with open(os.path.join(args.generic_dir, testName + ".py"), "w") as f:
        f.write(content)
    code = f"import slicer.testing; slicer.testing.runUnitTest([{os.path.abspath(args.generic_dir)!r}], {testName!r}); slicer.app.exit(0)"
    return run(slicerCommand(args, code, ["--no-main-window"]), args, f"generic {module}")


def runPythonUnitTest(args):
    directory = resolveDirectory(args.directory)
    command = [pythonSlicerPath(args), "-m", "unittest", "-v", *splitNames(args.testfiles)]
    return run(command, args, f"pyunit {' '.join(args.testfiles)}", cwd=directory)


def main(argv):
    parser = argparse.ArgumentParser(prog="StudioTestRunner.py", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("\n\n", 1)[1])
    parser.add_argument("--slicer", default=DEFAULT_SLICER, help=f"Slicer launcher (default: {DEFAULT_SLICER})")
    parser.add_argument("--pythonslicer", help="PythonSlicer executable for pyunit (default: found next to the build)")
    parser.add_argument("--slicer-args", default="", help="extra Slicer arguments (one string)")
    parser.add_argument("--home", help="HOME of the child process (for example a throwaway settings directory)")
    parser.add_argument("--generic-dir", default="/tmp/claude-0/generic", help="where generic tests are configured (default: /tmp/claude-0/generic)")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds per child process (default: 1800)")
    parser.add_argument("--no-xvfb", action="store_true", help="do not wrap the child in xvfb-run")
    subparsers = parser.add_subparsers(dest="command", required=True)
    unittestParser = subparsers.add_parser("unittest", help="slicer.testing.runUnitTest with --testing")
    unittestParser.add_argument("directory", help="test directory, or a module name")
    unittestParser.add_argument("names", nargs="+", help="test module names")
    unittestParser.set_defaults(function=runUnitTests)
    selftestParser = subparsers.add_parser("selftest", help="slicer.selfTests[MODULE]() with --testing")
    selftestParser.add_argument("module")
    selftestParser.set_defaults(function=runSelfTest)
    genericParser = subparsers.add_parser("generic", help="generic module test with --testing --no-main-window")
    genericParser.add_argument("module")
    genericParser.set_defaults(function=runGenericTest)
    pyunitParser = subparsers.add_parser("pyunit", help="PythonSlicer -m unittest in a directory")
    pyunitParser.add_argument("directory", help="working directory, or a module name (its source directory)")
    pyunitParser.add_argument("testfiles", nargs="+", help="test file(s), relative to the directory")
    pyunitParser.set_defaults(function=runPythonUnitTest)
    args = parser.parse_args(argv)
    try:
        return args.function(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

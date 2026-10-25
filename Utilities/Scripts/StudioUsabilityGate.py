#!/usr/bin/env python3
r"""Slicer Studio usability regression gate (USABILITY.md section 16): the walkthrough tasks and the review scenarios,
re-run headless with the actions counted from the input events that the driver sends.

Run inside Slicer, with a throwaway settings folder (arguments after ``--``)::

  HOME=$(mktemp -d) xvfb-run -a -s "-screen 0 1920x1080x24" ./Slicer --no-splash --python-script Utilities/Scripts/StudioUsabilityGate.py -- \
    --task all --wave 2 --out DIR [--size WxH] [--input auto|xdotool|synthetic] [--baseline] [--compare DIR/baseline.json]

Every task process saves its projects in a throwaway Slicer Studio folder (``SLICER_STUDIO_PROJECTS_ROOT`` = ``<its HOME>/Slicer
Studio``), so the first-start folder question (A-09) never blocks a run.

``--task`` takes ``all`` or task names (novice, nav, find-tools, find-tools-warm, classic, case, lnm, wrong-side, reopen,
dicom, esc-matrix, keyboard-layouts). Every task runs in its own Slicer process with a fresh settings folder (under a
nested xvfb of the requested size when xvfb-run exists), so that a task starts like the first launch and the counts do
not depend on the tasks before it (``--inprocess`` runs the tasks in this process, as the unit test does).

Outputs in ``DIR``: ``results.json`` ({task: {actions, target, official, pass, failedAssertions, steps, shots...}, "_meta": {...}}),
``results.md``, ``baseline.json`` (with ``--baseline``: the reference of later runs; failures are recorded, not blocking),
``shots/<task>/NN-name.png``, ``log/*.jsonl`` and ``data/`` (the fixtures).

Exit code 1 when a task exceeds its target for the wave (``targets.json``), an assertion that must pass from that wave
fails or a task raised; 0 otherwise (always 0 with ``--baseline``).
"""

import argparse
import datetime
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback

SCRIPT_PATH = os.path.abspath(globals().get("__file__") or sys.argv[0])
SCRIPT_DIR = os.path.dirname(SCRIPT_PATH)
SOURCE_DIR = os.path.dirname(os.path.dirname(SCRIPT_DIR))
#: Wall-clock limit of one task process, in seconds
TASK_PROCESS_TIMEOUT = 900


def parseArguments(argv):
    parser = argparse.ArgumentParser(description="Slicer Studio usability gate")
    parser.add_argument("--task", default="all", help="all, or comma-separated task names")
    parser.add_argument("--wave", type=int, default=1, help="gate wave: targets of USABILITY section 16 after this wave")
    parser.add_argument("--out", required=True, help="output folder")
    parser.add_argument("--size", default="", help="screen and window size WxH (default: the screen)")
    parser.add_argument("--input", default="auto", choices=("auto", "xdotool", "synthetic"))
    parser.add_argument("--baseline", action="store_true", help="record without failing and write baseline.json")
    parser.add_argument("--compare", default="", help="baseline.json of a previous run: the step lists are diffed in results.md")
    parser.add_argument("--inprocess", action="store_true", help="run the tasks in this process (no isolation, no nested X server)")
    parser.add_argument("--via-rc", action="store_true", help="(child) the process was started through SLICERRC")
    parser.add_argument("--data", default="", help="fixtures folder (default: OUT/data)")
    parser.add_argument("--result", default="", help="(child) file for the results of this process")
    parser.add_argument("--fast", action="store_true", help="skip the slow parts (first-run process, closing the application)")
    parser.add_argument("--no-nested-xvfb", action="store_true", help="run the task processes on the current display")
    parser.add_argument("--last-job", action="store_true", help="(child) this process runs one task: the task may close the application")
    return parser.parse_args(argv)


def inSlicer():
    try:
        import slicer
    except ImportError:
        return False
    return getattr(slicer, "app", None) is not None


def buildDescription():
    try:
        return subprocess.run(["git", "-C", SOURCE_DIR, "describe", "--always", "--dirty"], capture_output=True, text=True, check=False, timeout=20).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def selectedSpecs(names):
    from StudioUsabilityGate import tasks

    if names == ["all"]:
        return list(tasks.TASK_SPECS)
    return [tasks.taskSpec(name) for name in names]


#
# One process: run the tasks of this process
#


def runTasksInProcess(args, specs, onDone):
    """Run ``specs`` in this Slicer process; ``onDone(results, errors)`` gets the ``results.json`` entries."""
    import qt
    from StudioUsabilityGate import fixtures, harness, tasks

    size = None
    if args.size:
        width, height = (int(value) for value in args.size.lower().split("x"))
        size = (width, height)
    logName = "-".join(spec.name for spec in specs)[:60] or "gate"
    gate = harness.Gate(args.out, size=size, inputMode=args.input, wave=args.wave, logName=logName)
    gate.config.update({"viaRc": bool(args.via_rc), "fast": bool(args.fast)})
    dataDirectory = args.data or os.path.join(os.path.abspath(args.out), "data")
    gate.fixtures = fixtures.makeFixtures(dataDirectory)
    scheduler = tasks.runSpecs(gate, specs, args.wave, onDone, lastJob=args.last_job or len(specs) == 1)
    qt.QTimer.singleShot(1000, scheduler.start)
    return gate


def runChild(args):
    """Entry of a task process: run the tasks, write ``--result`` and exit Slicer."""
    import slicer

    names = [name for name in args.task.split(",") if name]
    try:
        specs = selectedSpecs(names)
    except Exception:
        print(traceback.format_exc(), flush=True)
        slicer.app.exit(2)
        return

    def done(results, errors):
        document = {"results": results, "errors": errors}
        if args.result:
            os.makedirs(os.path.dirname(os.path.abspath(args.result)), exist_ok=True)
            with open(args.result, "w") as stream:
                json.dump(document, stream, indent=1, default=str)
        slicer.app.exit(0)

    runTasksInProcess(args, specs, done)


#
# The orchestrator: one process per task
#


def launcherPath():
    import slicer

    launcher = slicer.app.launcherExecutableFilePath
    return launcher() if callable(launcher) else launcher


def screenSize():
    import slicer

    window = slicer.util.mainWindow()
    screen = window.screen() if window is not None and callable(getattr(window, "screen", None)) else None
    if screen is not None:
        geometry = screen.geometry
        geometry = geometry() if callable(geometry) else geometry
        return geometry.width(), geometry.height()
    return 1920, 1080


def runOneProcess(args, spec, size, dataDirectory, outDirectory, homeDirectory, last=True):
    """Run one task in a fresh Slicer process. Return ``(resultDocument or None, message)``."""
    resultPath = os.path.join(outDirectory, "tasks", f"{spec.name}.json")
    if os.path.exists(resultPath):
        os.remove(resultPath)
    childArguments = ["--task", spec.name, "--out", outDirectory, "--wave", str(args.wave), "--input", args.input, "--data", dataDirectory,
                      "--result", resultPath, "--inprocess", "--size", f"{size[0]}x{size[1]}", "--last-job"]
    if args.fast:
        childArguments.append("--fast")
    environment = dict(os.environ)
    environment["HOME"] = homeDirectory
    environment["XDG_CONFIG_HOME"] = os.path.join(homeDirectory, ".config")
    for key in ("SLICER_STUDIO_WEB", "SLICERRC", "SLICER_STUDIO_PROJECTS"):
        environment.pop(key, None)
    # Projects (A-09) go to a throwaway folder of the task's home, and the folder chooser is never shown
    environment["SLICER_STUDIO_PROJECTS_ROOT"] = os.path.join(homeDirectory, "Slicer Studio")
    command = [launcherPath(), "--no-splash"]
    if spec.viaRc:
        os.makedirs(os.path.join(outDirectory, "rc"), exist_ok=True)
        rcPath = os.path.join(outDirectory, "rc", f"{spec.name}.py")
        with open(rcPath, "w") as stream:
            stream.write(
                "import sys\n"
                f"sys.argv = {[SCRIPT_PATH, *childArguments, '--via-rc']!r}\n"
                f"_gateScript = {SCRIPT_PATH!r}\n"
                "exec(compile(open(_gateScript).read(), _gateScript, 'exec'), {'__name__': '__main__', '__file__': _gateScript})\n")
        environment["SLICERRC"] = rcPath
    else:
        command += ["--python-script", SCRIPT_PATH, "--", *childArguments]
    nested = not args.no_nested_xvfb and shutil.which("xvfb-run") is not None and os.environ.get("DISPLAY")
    if nested:
        command = ["xvfb-run", "-a", "-s", f"-screen 0 {size[0]}x{size[1]}x24", *command]
    logPath = os.path.join(outDirectory, "log", f"process-{spec.name}.txt")
    os.makedirs(os.path.dirname(logPath), exist_ok=True)
    started = time.time()
    with open(logPath, "w") as logStream:
        # Own process group: on a hang the whole tree (xvfb-run, Xvfb, Slicer) is killed, not only the wrapper
        process = subprocess.Popen(command, env=environment, stdout=logStream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            exitCode = process.wait(timeout=TASK_PROCESS_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
            process.wait()
            exitCode = 124
    message = f"exit code {exitCode} after {time.time() - started:.0f} s (log: {logPath})"
    if os.path.isfile(resultPath):
        with open(resultPath) as stream:
            return json.load(stream), message
    return None, message


def runOrchestrator(args):
    import slicer
    from StudioUsabilityGate import fixtures, harness

    names = [name for name in args.task.split(",") if name]
    specs = selectedSpecs(names)
    outDirectory = os.path.abspath(args.out)
    os.makedirs(outDirectory, exist_ok=True)
    size = tuple(int(value) for value in args.size.lower().split("x")) if args.size else screenSize()
    dataDirectory = os.path.abspath(args.data) if args.data else os.path.join(outDirectory, "data")
    fixtures.makeFixtures(dataDirectory)
    targets = harness.loadTargets()
    results, errors, messages = {}, {}, {}
    sessions = {}
    for spec in specs:
        homeDirectory = os.path.join(outDirectory, "home", spec.name)
        if os.path.isdir(homeDirectory):
            shutil.rmtree(homeDirectory)
        coldWarning = None
        if spec.homeFrom and spec.homeFrom in sessions:
            shutil.copytree(sessions[spec.homeFrom], homeDirectory)
        else:
            os.makedirs(homeDirectory)
            if spec.homeFrom:
                coldWarning = f"{spec.name} starts from the settings of {spec.homeFrom}, which did not run in this invocation: this is a cold session, not a warm one"
                print(f"GATE WARNING {coldWarning}", flush=True)
        print(f"GATE running {spec.name} ...", flush=True)
        document, message = runOneProcess(args, spec, size, dataDirectory, outDirectory, homeDirectory)
        sessions[spec.name] = homeDirectory
        messages[spec.name] = message
        print(f"GATE {spec.name}: {message}", flush=True)
        if document is None:
            errors[spec.name] = "the task process produced no result: " + message
            results[spec.name] = {"title": spec.title, "actions": None, "pass": False, "failedAssertions": ["task-completed: " + errors[spec.name]],
                                  "failures": [errors[spec.name]], "error": errors[spec.name], "steps": [], "shots": [], "metrics": {}, "assertions": {},
                                  "expectedFailures": [], "warnings": [], "notes": {}, "target": harness.targetFor(targets, spec.name, args.wave)}
            continue
        results.update(document["results"])
        errors.update(document.get("errors", {}))
        if coldWarning and spec.name in results:
            results[spec.name].setdefault("warnings", []).append(coldWarning)
    meta = {"wave": args.wave, "baseline": bool(args.baseline), "build": buildDescription(), "date": datetime.datetime.now().isoformat(timespec="seconds"),
            "size": f"{size[0]}x{size[1]}", "input": args.input, "processes": messages}
    previous = None
    if args.compare and os.path.isfile(args.compare):
        with open(args.compare) as stream:
            previous = json.load(stream)

    problems = harness.evaluateResults(results, args.wave, baselineMode=args.baseline)
    with open(os.path.join(outDirectory, "results.json"), "w") as stream:
        json.dump({**results, "_meta": meta}, stream, indent=1, default=str)
    with open(os.path.join(outDirectory, "results.md"), "w") as stream:
        stream.write(harness.renderMarkdown(results, meta, previous))
    if args.baseline:
        with open(os.path.join(outDirectory, "baseline.json"), "w") as stream:
            json.dump(harness.baselineDocument(results, meta), stream, indent=1, default=str)
    print("GATE counts: " + ", ".join(f"{name}={result.get('actions')}" for name, result in results.items()), flush=True)
    for problem in problems:
        print("GATE BLOCKING " + problem, flush=True)
    exitCode = 1 if problems and not args.baseline else 0
    print(f"GATE exit code {exitCode}", flush=True)
    slicer.app.exit(exitCode)


def main(argv):
    args = parseArguments(argv)
    if SCRIPT_DIR not in sys.path:
        sys.path.insert(0, SCRIPT_DIR)
    if not inSlicer():
        print("Run inside Slicer: Slicer --python-script StudioUsabilityGate.py -- --task all --out DIR", file=sys.stderr)
        return 2
    if args.inprocess:
        runChild(args)
    else:
        runOrchestrator(args)
    return 0


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    code = main(arguments)
    if code:
        sys.exit(code)

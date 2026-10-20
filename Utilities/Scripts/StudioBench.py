#!/usr/bin/env python3
r"""Slicer Studio benchmark harness: the before/after performance gate of every work package.

In-Slicer mode measures interactive costs and writes a JSON file::

  xvfb-run -a -s "-screen 0 1920x1080x24" ./Slicer --no-splash --python-script Utilities/Scripts/StudioBench.py -- \
    --out F.json [--only m1,m2] [--check] [--compare prev.json] [--mrhead PATH]

System-Python mode measures startup through child processes, merges runs (per-metric median; the VM
is noisy, so compare medians of 3 or more runs), or evaluates an existing JSON::

  python3 Utilities/Scripts/StudioBench.py --startup --slicer /opt/S-r/Slicer-build/Slicer --runs 3 --out F.json [--home DIR]
  python3 Utilities/Scripts/StudioBench.py --merge run1.json run2.json run3.json --out F.json
  python3 Utilities/Scripts/StudioBench.py --input F.json [--check] [--compare prev.json]

Startup reports the phases of ``--report-startup-timing``. Their total, ``startup_total_ms``, is
startup to interactive Library: the first show of the main window emits ``startupCompleted``, whose
slots (the Studio apply among them) run synchronously within the "Showing the main window" phase.
When Studio is applied, ``startup_studio_apply_ms`` reports the duration of the apply, which is part
of that total.

The JSON holds ``{"metrics": {...}, "errors": {...}, "details": {...}, "meta": {...}}``. Every metric
is a number for which lower is better, or null when its measurement failed (the reason is then in
"errors"). Metrics that do not apply to the running build (for example Studio metrics before the
Studio module exists) are listed in ``details["skipped"]`` instead.

``--check`` exits 1 when a metric exceeds its budget (``BUDGETS``, SPEC section 8.2; a relative
budget is evaluated against the ``--compare`` JSON). ``--compare`` prints the metrics that regressed
by more than ``--tolerance`` (15%) versus a previous JSON and exits 1 if there are any. Differences
smaller than ``--min-delta-ms`` are treated as noise.

Null means FAIL for the gate: a metric that is null, or that has an entry in "errors" (a failed
startup run, or a run of a ``--merge`` input), is a failed measurement and makes every mode exit 1,
with or without ``--check`` / ``--compare``. So does a metric that the ``--compare`` JSON has a value
for but that this run skipped. Metrics that are merely absent (not selected with ``--only``) are not
failures.

``--only`` takes metric names or measurement group names (see ``--list``). A group always measures
all of its metrics, but only the requested metrics are written.

Exit codes: 0 success, 1 failed measurement, budget exceeded or regression found, 2 usage or
internal error.
"""

import argparse
import contextlib
import datetime
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import traceback

SCRIPT_PATH = os.path.abspath(globals().get("__file__") or sys.argv[0])
SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_PATH)))
DEFAULT_MRHEAD_PATH = "/tmp/claude-0/understand/data/MRHead.nrrd"

# Budgets of SPEC section 8.2, in the unit of the metric (suffix _ms or _s; no suffix = count).
# A dict value {"relative": r} means "at most r above the value of the --compare JSON".
# Not numeric, therefore not listed: "LNM GUI: frame rate unaffected during compute (subprocess)".
BUDGETS = {
    # Startup to interactive Library (Studio on): waves 1-3; 2000 after W4-03. The --report-startup-timing total
    # includes the Studio apply: the "Showing the main window" phase ends after the startupCompleted slots return.
    "startup_total_ms": 2700,
    # Studio apply in startupCompleted: shell.applyDurationMs, read in-Slicer and in each --startup run
    "studio_apply_ms": 150,
    "startup_studio_apply_ms": 150,
    "workspace_switch_ms": 50,  # Workspace switch (warm, no layout change)
    # Warm module switch: at most +10% of baseline. It varies more than 10% between runs on a loaded VM: evaluate
    # medians of 3 or more runs (--merge) on both sides; evaluate() warns otherwise.
    "module_switch_warm_ms": {"relative": 0.10},
    "hover_ms": 1.0,  # Hover (Qt side), Studio, per event
    "scroll_step_ms": 5.0,  # Wheel step (event loop)
    "noop_modified_ms": 3.0,  # No-op sliceNode.Modified
    "stroke_release_256_3d_ms": 200,  # Stroke release, 256^3 + 3D, 7 segments (120 with GPU labelmap 3D)
    "stroke_release_mni_intensity_ms": 35,  # Intensity-masked stroke, MNI 1 mm
    "smoothing_median3_ms": 150,  # Smoothing / Islands / Margin, lesion
    "islands_keep_largest_ms": 150,
    "margin_2mm_ms": 150,
    "paint_hover_surfaces_ms": 10,  # Paint hover with surfaces
    "draw_apply_ms": 40,  # Draw apply (W4-01)
    "lesion_card_refresh_ms": 15,  # Lesion card refresh
    "lnm_synthetic_s": 10,  # LNM synthetic end to end (CLI)
}

# Measurement groups: (group name, metrics it produces, InSlicerBench method).
GROUPS = (
    ("module_switch", ("module_switch_cold_ms", "module_switch_warm_ms", "module_switch_warm_select_only_ms",
                       "segment_editor_first_open_ms"), "measureModuleSwitch"),
    ("studio", ("studio_apply_ms", "workspace_switch_ms"), "measureStudio"),
    ("layout", ("layout_switch_ms",), "measureLayoutSwitch"),
    ("slices", ("hover_ms", "scroll_step_ms", "noop_modified_ms", "slice_logic_modified_per_step",
                "slice_logic_modified_per_step_fg", "render_counts_other_views", "scroll_step_3d_ms"), "measureSlices"),
    ("paint_256", ("stroke_release_256_3d_ms", "surfaces_regenerated", "paint_hover_surfaces_ms", "undo_surfaces_ms"), "measurePaint256"),
    ("paint_mni", ("stroke_release_mni_ms", "stroke_release_mni_intensity_ms", "draw_apply_ms"), "measurePaintMNI"),
    ("segment_ops", ("smoothing_median3_ms", "islands_keep_largest_ms", "margin_2mm_ms"), "measureSegmentOperations"),
    ("effect_gui", ("effect_gui_refresh_ms",), "measureEffectGuiRefresh"),
    ("lnm", ("lnm_synthetic_s",), "measureLesionNetworkMapping"),
)

# Run by --startup through --python-script: Slicer runs it once the event loop processes the command line, so
# after startupCompleted and the Studio apply (both run within the "Showing the main window" phase).
STARTUP_PROBE_SCRIPT = """
try:
    from StudioLib import shell
    studio = shell.instance()
except ImportError:
    studio = None
print("StudioBench startup probe: studio_apply_ms=%s" % getattr(studio, "applyDurationMs", None), flush=True)
"""

STARTUP_PHASES = {
    "Before the process entry point, loading the application's own libraries": "startup_pre_entry_ms",
    "Initializing the application": "startup_init_app_ms",
    "Registering modules": "startup_register_ms",
    "Instantiating modules": "startup_instantiate_ms",
    "Initializing the user interface": "startup_init_ui_ms",
    "Loading modules": "startup_load_ms",
    "Showing the main window": "startup_show_ms",
}


#
# Evaluation (shared by all modes)
#

def minimumDelta(name, minDeltaMs):
    """Smallest increase of a metric that is not considered noise."""
    if name.endswith("_ms"):
        return minDeltaMs
    if name.endswith("_s"):
        return minDeltaMs / 1000.0
    return 0.0


def checkBudgets(metrics, previousMetrics=None):
    """Return (failures, lines) for the metrics that have a budget.

    Null metrics are not evaluated here: they are failed measurements (see measurementFailures).
    """
    failures = []
    lines = []
    for name, budget in BUDGETS.items():
        value = metrics.get(name)
        if value is None:
            continue
        if isinstance(budget, dict):
            previous = (previousMetrics or {}).get(name)
            if previous is None:
                reason = "needs --compare" if previousMetrics is None else "no value in the --compare JSON"
                lines.append(f"  {name:40s} {value:12.3f}  relative budget +{budget['relative']:.0%}: not checked ({reason})")
                continue
            limit = previous * (1.0 + budget["relative"])
            budgetText = f"<= {limit:.3f} (+{budget['relative']:.0%} of {previous:.3f})"
        else:
            limit = budget
            budgetText = f"<= {limit}"
        ok = value <= limit
        lines.append(f"  {name:40s} {value:12.3f}  {budgetText:32s} {'ok' if ok else 'OVER BUDGET'}")
        if not ok:
            failures.append(name)
    return failures, lines


def compareMetrics(metrics, previousMetrics, tolerance, minDeltaMs):
    """Return (regressions, lines): metrics that increased by more than tolerance versus previousMetrics.

    Metrics that are null now but had a value before are listed, but not returned: they are failed
    measurements (see measurementFailures).
    """
    regressions = []
    lines = []
    for name in sorted(metrics):
        value = metrics[name]
        previous = previousMetrics.get(name)
        if previous is None:
            continue
        if value is None:
            lines.append(f"  {name:40s} {previous:12.3f} ->         null            MEASUREMENT FAILED")
            continue
        delta = value - previous
        ratio = (delta / previous) if previous else (float("inf") if delta > 0 else 0.0)
        regressed = delta > minimumDelta(name, minDeltaMs) and ratio > tolerance
        lines.append(f"  {name:40s} {previous:12.3f} -> {value:12.3f}  {ratio * 100.0:+8.1f}%  {'REGRESSION' if regressed else ''}")
        if regressed:
            regressions.append(name)
    return regressions, lines


def measurementFailures(result):
    """Names of the failed measurements of a result: its null metrics and every key of its "errors".

    Error keys can also name something other than a metric (for example "run2" of --startup).
    Metrics that are absent or skipped are not failures.
    """
    names = {name for name, value in result.get("metrics", {}).items() if value is None}
    names.update(result.get("errors", {}))
    return sorted(names)


def lostMeasurements(result, previousMetrics):
    """Metrics that have a value in previousMetrics but that this result skipped instead of measuring.

    For example the Studio metrics after a change that makes StudioLib.shell.instance() return None.
    """
    metrics = result.get("metrics", {})
    skipped = result.get("details", {}).get("skipped", {})
    return sorted(name for name in skipped if name not in metrics and previousMetrics.get(name) is not None)


def loadCompareResult(args):
    """The --compare JSON, or None without --compare. Raises OSError or ValueError if it cannot be read."""
    if not args.compare:
        return None
    with open(args.compare) as f:
        return json.load(f)


def relativeBudgetWarnings(result, previousResult):
    """Warnings for relative budgets evaluated on single runs, which the VM noise makes unreliable."""
    names = [name for name, budget in BUDGETS.items() if isinstance(budget, dict) and result.get("metrics", {}).get(name) is not None]
    if not names or previousResult is None:
        return []
    single = [label for label, data in (("this result", result), ("the --compare JSON", previousResult))
              if data.get("meta", {}).get("mode") != "merge"]
    if not single:
        return []
    return [(f"Warning: {', '.join(names)} relative budget evaluated on a single run ({' and '.join(single)}); "
             "use --merge of 3 or more runs on both sides")]


def evaluate(result, args):
    """Print the metrics and the evaluation (failed measurements, --check, --compare). Returns the exit code."""
    previousResult = loadCompareResult(args)
    previousMetrics = previousResult.get("metrics", {}) if previousResult is not None else None
    metrics = result.get("metrics", {})
    errors = result.get("errors", {})
    print("Metrics:")
    for name in sorted(metrics):
        value = metrics[name]
        errorLines = str(errors.get(name, "")).strip().splitlines()
        print(f"  {name:40s} {'        null' if value is None else f'{value:12.3f}'}  {errorLines[-1] if errorLines else ''}")
    for name in sorted(set(errors) - set(metrics)):  # errors that are not about a metric, for example a failed startup run
        errorLines = str(errors[name]).strip().splitlines()
        print(f"  {name:40s} {'error':>12s}  {errorLines[-1] if errorLines else ''}")
    # Null means FAIL: a measurement that broke must not pass the gate silently
    summary = []
    failed = measurementFailures(result)
    if failed:
        summary.append("Measurement failed: " + ", ".join(failed))
    if args.check:
        failures, lines = checkBudgets(metrics, previousMetrics)
        print("Budgets (SPEC 8.2):")
        print("\n".join(lines) if lines else "  no measured metric has a budget")
        for line in relativeBudgetWarnings(result, previousResult):
            print(line)
        if failures:
            summary.append("Over budget: " + ", ".join(failures))
    if previousMetrics is not None:
        regressions, lines = compareMetrics(metrics, previousMetrics, args.tolerance, args.min_delta_ms)
        lost = lostMeasurements(result, previousMetrics)
        skipped = result.get("details", {}).get("skipped", {})
        lines += [f"  {name:40s} {previousMetrics[name]:12.3f} ->      skipped  MEASUREMENT FAILED ({skipped[name]})" for name in lost]
        print(f"Comparison with {args.compare} (tolerance {args.tolerance:.0%}, noise floor {args.min_delta_ms} ms):")
        print("\n".join(lines) if lines else "  no common metric")
        if lost:
            summary.append("Measurement failed (measured in the --compare JSON, skipped now): " + ", ".join(lost))
        if regressions:
            summary.append("Regressions: " + ", ".join(regressions))
    for line in summary:
        print(line)
    return 1 if summary else 0


def gitRevision():
    try:
        rev = subprocess.run(["git", "-C", SOURCE_DIR, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=30, check=False).stdout.strip()
        dirty = subprocess.run(["git", "-C", SOURCE_DIR, "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, timeout=30, check=False).stdout.strip()
        return rev + ("-dirty" if dirty else "")
    except Exception:
        return None


def baseMeta(argv):
    return {
        "git_rev": gitRevision(),
        "utc": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "argv": list(argv),
        "host": platform.node(),
        "cpu_count": os.cpu_count(),
        "loadavg_start": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
    }


def writeResult(path, result):
    if not path:
        return
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        json.dump(result, f, indent=1, sort_keys=True)
    print(f"Wrote {path}", flush=True)


def parseArguments(argv, inSlicer):
    parser = argparse.ArgumentParser(
        prog="StudioBench.py", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", help="output JSON file")
    parser.add_argument("--only", help="comma-separated metric or group names (default: all)")
    parser.add_argument("--list", action="store_true", help="list measurement groups, metrics and budgets, then exit")
    parser.add_argument("--check", action="store_true", help="exit 1 if a measured metric exceeds its budget")
    parser.add_argument("--compare", help="previous JSON; exit 1 if a metric regressed by more than --tolerance")
    parser.add_argument("--tolerance", type=float, default=0.15, help="allowed relative regression for --compare (default: 0.15)")
    parser.add_argument("--min-delta-ms", type=float, default=0.25, help="increases below this many ms are noise for --compare (default: 0.25)")
    parser.add_argument("--mrhead", default=DEFAULT_MRHEAD_PATH, help="MRHead volume file; SampleData MRHead is used if it does not exist")
    if not inSlicer:
        parser.add_argument("--input", help="evaluate an existing JSON (--check / --compare) without measuring")
        parser.add_argument("--merge", nargs="+", metavar="JSON", help="write the per-metric median of several result files to --out")
        parser.add_argument("--startup", action="store_true", help="measure startup with --report-startup-timing --exit-after-startup")
        parser.add_argument("--slicer", default="/opt/S-r/Slicer-build/Slicer", help="Slicer launcher for --startup")
        parser.add_argument("--runs", type=int, default=3, help="startup runs; the median of each phase is reported (default: 3)")
        parser.add_argument("--home", help="HOME of the child processes (for example a throwaway settings directory)")
        parser.add_argument("--slicer-args", default="", help="extra Slicer arguments for --startup (space-separated)")
        parser.add_argument("--timeout", type=float, default=600, help="seconds per startup run (default: 600)")
        parser.add_argument("--no-xvfb", action="store_true", help="do not wrap the child in xvfb-run")
    return parser.parse_args(argv)


def listMeasurements():
    print("Measurement groups (in-Slicer mode):")
    for group, metrics, _method in GROUPS:
        print(f"  {group}: {', '.join(metrics)}")
    print("Startup mode: startup_total_ms (startup to interactive Library, including the Studio apply), startup_wall_ms, "
          "startup_studio_apply_ms (only when Studio is applied), " + ", ".join(STARTUP_PHASES.values()))
    print("Budgets (SPEC 8.2):")
    for name, budget in BUDGETS.items():
        print(f"  {name}: {budget}")


#
# In-Slicer measurements
#

class InSlicerBench:
    """Runs the measurement groups inside a running Slicer application."""

    def __init__(self, args):
        self.args = args
        self.metrics = {}
        self.errors = {}
        self.details = {"skipped": {}}
        self._mrHeadNode = None

    # -- helpers

    @staticmethod
    def pump(count=2):
        import slicer
        for _ in range(count):
            slicer.app.processEvents()

    @staticmethod
    def idleWait(msec):
        """Run the event loop for msec without busy spinning."""
        import qt
        loop = qt.QEventLoop()
        qt.QTimer.singleShot(msec, loop.quit)
        loop.exec_()

    @staticmethod
    def elapsedMs(startTime):
        return (time.perf_counter() - startTime) * 1000.0

    def record(self, name, value, digits=3):
        self.metrics[name] = round(float(value), digits)
        print(f"BENCH {name} = {self.metrics[name]}", flush=True)

    def fail(self, name, message):
        self.metrics[name] = None
        self.errors[name] = message
        print(f"BENCH {name} FAILED: {message.strip().splitlines()[-1] if message.strip() else message}", flush=True)

    def skip(self, name, reason):
        self.details["skipped"][name] = reason
        print(f"BENCH {name} skipped: {reason}", flush=True)

    def attempt(self, names, function):
        """Run function; metrics of names that it did not record become null with the error."""
        try:
            function()
        except Exception:
            message = traceback.format_exc()[-3000:]
            for name in names:
                if name not in self.metrics and name not in self.details["skipped"]:
                    self.fail(name, message)

    @staticmethod
    def studioShell():
        """The live Studio shell, or None (no Studio module, Classic, testing, no main window)."""
        try:
            from StudioLib import shell
        except ImportError:
            return None
        try:
            return shell.instance()
        except Exception:
            return None

    def prepareViews(self):
        """Make sure slice and 3D views render (the Studio Library workspace pauses rendering)."""
        shell = self.studioShell()
        if shell is not None:
            try:
                if shell.currentWorkspace() == "library":
                    shell.switchTo("draw")
                    self.pump(4)
            except Exception:
                traceback.print_exc()

    def clearScene(self):
        import slicer
        slicer.mrmlScene.Clear(0)
        self._mrHeadNode = None
        self.pump(4)

    def mrHead(self):
        """Load MRHead once (kept until the scene is cleared) and show it in the slice views."""
        import slicer
        if self._mrHeadNode is not None and slicer.mrmlScene.IsNodePresent(self._mrHeadNode):
            return self._mrHeadNode
        startTime = time.perf_counter()
        if self.args.mrhead and os.path.exists(self.args.mrhead):
            node = slicer.util.loadVolume(self.args.mrhead, {"name": "MRHead"})
            self.details["mrhead_source"] = self.args.mrhead
        else:
            import SampleData
            node = SampleData.SampleDataLogic().downloadSample("MRHead")
            self.details["mrhead_source"] = "SampleData"
        slicer.util.setSliceViewerLayers(background=node, foreground=None, label=None, fit=True)
        self.pump(4)
        self.details["mrhead_load_ms"] = round(self.elapsedMs(startTime), 1)
        self._mrHeadNode = node
        return node

    def segmentEditor(self, volumeNode, segmentationName="BenchSegmentation"):
        """Select the Segment Editor and set it up with an empty segmentation for volumeNode."""
        import slicer
        slicer.util.selectModule("SegmentEditor")
        self.pump(4)
        editor = slicer.modules.SegmentEditorWidget.editor
        segmentationNode = editor.segmentationNode()
        if segmentationNode is None or segmentationNode.GetSegmentation().GetNumberOfSegments() > 0:
            segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", segmentationName)
            segmentationNode.CreateDefaultDisplayNodes()
        segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(volumeNode)
        editor.setSegmentationNode(segmentationNode)
        editor.setSourceVolumeNode(volumeNode)
        self.pump(4)
        return editor, segmentationNode

    @staticmethod
    def addVolume(array, name, spacing=(1.0, 1.0, 1.0), origin=(0.0, 0.0, 0.0)):
        import slicer
        volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", name)
        volumeNode.SetSpacing(*spacing)
        volumeNode.SetOrigin(*origin)
        slicer.util.updateVolumeFromArray(volumeNode, array)
        volumeNode.CreateDefaultDisplayNodes()
        return volumeNode

    @staticmethod
    def rasToXY(sliceNode, ras):
        import vtk
        rasToXY = vtk.vtkMatrix4x4()
        rasToXY.DeepCopy(sliceNode.GetXYToRAS())
        rasToXY.Invert()
        xy = rasToXY.MultiplyPoint([ras[0], ras[1], ras[2], 1.0])
        return round(xy[0]), round(xy[1])

    @staticmethod
    def xyToRAS(sliceNode, xy):
        ras = sliceNode.GetXYToRAS().MultiplyPoint([float(xy[0]), float(xy[1]), 0.0, 1.0])
        return ras[:3]

    def timeUntilVisible(self, view, action):
        """Time of action() plus one render of view, that is, until its result is visible in view (ms).

        The render is forced: a scheduled render waits for a 60 fps timer, which would make the result
        depend on timing. For the same reason pending renders are completed before the timing starts.
        The events that the action queued are processed afterwards, outside the timing.
        """
        self.idleWait(30)
        startTime = time.perf_counter()
        action()
        view.forceRender()
        elapsed = self.elapsedMs(startTime)
        self.pump(2)
        return elapsed

    def stroke(self, interactor, points, view):
        """Paint a stroke through the interactor; returns the release time (until visible in view) in ms."""
        import slicer
        import vtk
        interactor.SetEventPosition(*points[0])
        interactor.InvokeEvent(vtk.vtkCommand.MouseMoveEvent)
        interactor.InvokeEvent(vtk.vtkCommand.LeftButtonPressEvent)
        for point in points[1:]:
            interactor.SetEventPosition(*point)
            interactor.InvokeEvent(vtk.vtkCommand.MouseMoveEvent)
            slicer.app.processEvents()
        return self.timeUntilVisible(view, lambda: interactor.InvokeEvent(vtk.vtkCommand.LeftButtonReleaseEvent))

    def hover(self, interactor, center, count=30):
        """Move the mouse over a view; returns the mean time per move (move + one processEvents) in ms."""
        import slicer
        import vtk
        startTime = time.perf_counter()
        for i in range(count):
            interactor.SetEventPosition(center[0] + (i % 20) * 3, center[1])
            interactor.InvokeEvent(vtk.vtkCommand.MouseMoveEvent)
            slicer.app.processEvents()
        return self.elapsedMs(startTime) / count

    def renderCounters(self, views):
        """Count render window StartEvents of the named views. Returns (counts, removeObservers)."""
        import vtk
        counts = dict.fromkeys(views, 0)
        observations = []
        for name, view in views.items():
            renderWindow = view.renderWindow()

            def callback(caller, event, name=name):
                counts[name] += 1
            observations.append((renderWindow, renderWindow.AddObserver(vtk.vtkCommand.StartEvent, callback)))

        def removeObservers():
            for renderWindow, tag in observations:
                renderWindow.RemoveObserver(tag)
        return counts, removeObservers

    @contextlib.contextmanager
    def uncappedRendering(self):
        """Render each view at the first event processing after its render request, instead of at most at 60 fps.

        With the 60 fps cap, how many requests one render serves depends on timing, so render counts
        and per-event costs would vary from run to run.
        """
        views = list(self.allViews().values())
        rates = [view.maximumUpdateRate for view in views]
        for view in views:
            view.maximumUpdateRate = 0.0
        try:
            yield
        finally:
            for view, rate in zip(views, rates, strict=True):
                view.maximumUpdateRate = rate

    @staticmethod
    def allViews():
        import slicer
        layoutManager = slicer.app.layoutManager()
        views = {name: layoutManager.sliceWidget(name).sliceView() for name in layoutManager.sliceViewNames()}
        for index in range(layoutManager.threeDViewCount):
            views[f"3D{index}"] = layoutManager.threeDWidget(index).threeDView()
        return views

    # -- groups

    def measureModuleSwitch(self):
        import slicer
        self.mrHead()
        modules = ["SegmentEditor", "Volumes", "Data", "Markups", "Models"]
        self.details["segment_editor_widget_precreated"] = hasattr(slicer.modules, "SegmentEditorWidget")
        cold = {}
        for name in modules:
            startTime = time.perf_counter()
            slicer.util.selectModule(name)
            self.pump(2)
            cold[name] = self.elapsedMs(startTime)
            if name == "SegmentEditor":
                # First open also includes the work that entering the module queued (source volume, effects).
                self.pump(8)
                self.record("segment_editor_first_open_ms", self.elapsedMs(startTime))
        self.record("module_switch_cold_ms", statistics.median(cold.values()))
        warm = {name: [] for name in modules}
        selectOnly = []
        for repetition in range(5):
            for name in modules:
                startTime = time.perf_counter()
                slicer.util.selectModule(name)
                selectOnly.append(self.elapsedMs(startTime))
                self.pump(2)
                warm[name].append(self.elapsedMs(startTime))
        self.record("module_switch_warm_ms", statistics.median([value for values in warm.values() for value in values]))
        # selectModule alone, without the event processing (panel layout, view resize and render) that follows it.
        # This is what the SPEC 8.1 warm baseline (4-6 ms) measured; it is much less noisy than module_switch_warm_ms.
        self.record("module_switch_warm_select_only_ms", statistics.median(selectOnly))
        self.details["module_switch_cold_ms_by_module"] = {name: round(value, 2) for name, value in cold.items()}
        self.details["module_switch_warm_ms_by_module"] = {name: round(statistics.median(values), 2) for name, values in warm.items()}
        self.details["module_switch_warm_select_only_ms"] = round(statistics.median(selectOnly), 2)

    def measureStudio(self):
        shell = self.studioShell()
        if shell is None:
            for name in ("studio_apply_ms", "workspace_switch_ms"):
                self.skip(name, "StudioLib.shell.instance() is not available")
            return

        def applyDuration():
            duration = getattr(shell, "applyDurationMs", None)
            if callable(duration):
                duration = duration()
            if duration is None:
                raise RuntimeError("the Studio shell does not report applyDurationMs")
            self.record("studio_apply_ms", duration)
        self.attempt(["studio_apply_ms"], applyDuration)

        def workspaceSwitch():
            for key in ("draw", "library"):
                shell.switchTo(key)
                self.pump(4)
            times = {"draw": [], "library": []}
            for repetition in range(5):
                for key in ("draw", "library"):
                    startTime = time.perf_counter()
                    shell.switchTo(key)
                    self.pump(2)
                    times[key].append(self.elapsedMs(startTime))
            self.record("workspace_switch_ms", statistics.median(times["draw"] + times["library"]))
            self.details["workspace_switch_ms_by_workspace"] = {key: round(statistics.median(values), 2) for key, values in times.items()}
        self.attempt(["workspace_switch_ms"], workspaceSwitch)
        shell.switchTo("draw")
        self.pump(4)

    def measureLayoutSwitch(self):
        import slicer
        self.prepareViews()
        self.mrHead()
        layoutManager = slicer.app.layoutManager()
        layouts = {"FourUp": slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView, "Conventional": slicer.vtkMRMLLayoutNode.SlicerLayoutConventionalView}
        layoutManager.setLayout(layouts["Conventional"])
        self.pump(4)
        times = {name: [] for name in layouts}
        for repetition in range(3):
            for name, layout in layouts.items():
                startTime = time.perf_counter()
                layoutManager.setLayout(layout)
                self.pump(2)
                times[name].append(self.elapsedMs(startTime))
        self.record("layout_switch_ms", statistics.median(times["FourUp"] + times["Conventional"]))
        self.details["layout_switch_ms_to"] = {name: round(statistics.median(values), 2) for name, values in times.items()}

    def measureSlices(self):
        import numpy as np
        import vtk
        import slicer
        self.prepareViews()
        layoutManager = slicer.app.layoutManager()
        layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutConventionalView)
        volumeNode = self.mrHead()
        slicer.util.setSliceViewerLayers(background=volumeNode, foreground=None, label=None, fit=True)
        self.pump(4)
        self.idleWait(200)
        redWidget = layoutManager.sliceWidget("Red")
        redNode = redWidget.mrmlSliceNode()
        redLogic = redWidget.sliceLogic()
        redView = redWidget.sliceView()
        offset0 = redLogic.GetSliceOffset()
        # The metrics of this group are measured with the Red slice not shown in 3D (the default of a new scene, and the
        # state of the baselines). Studio shows it in the first 3D view of a session (U-06): then the 3D view has to render
        # the moving slice at every step, which scroll_step_3d_ms measures.
        sliceVisible = redNode.GetSliceVisible()
        self.details["red_slice_visible_in_3d_before"] = bool(sliceVisible)
        redNode.SetSliceVisible(False)
        self.pump(4)
        crosshair = slicer.mrmlScene.GetFirstNodeByClass("vtkMRMLCrosshairNode")
        centerX, centerY = redView.width // 2, redView.height // 2
        self.details["red_view_size"] = [redView.width, redView.height]

        def hover():
            def burst(count):
                startTime = time.perf_counter()
                for i in range(count):
                    crosshair.SetCursorPositionXYZ([centerX - 60 + (i % 120), centerY - 40 + ((i * 7) % 80), 0], redNode)
                    slicer.app.processEvents()
                return self.elapsedMs(startTime) / count
            burst(50)
            self.record("hover_ms", statistics.median([burst(300) for _ in range(5)]))
            self.idleWait(100)
        self.attempt(["hover_ms"], hover)

        def scroll():
            def run():
                startTime = time.perf_counter()
                for i in range(100):
                    redLogic.SetSliceOffset(offset0 - 50 + i)
                    slicer.app.processEvents()
                return self.elapsedMs(startTime) / 100
            run()
            self.record("scroll_step_ms", statistics.median([run() for _ in range(3)]))
            redLogic.SetSliceOffset(offset0)
            self.idleWait(100)
        self.attempt(["scroll_step_ms"], scroll)

        def noopModified():
            def run():
                startTime = time.perf_counter()
                for i in range(50):
                    redNode.Modified()
                    redView.forceRender()
                return self.elapsedMs(startTime) / 50
            run()
            self.record("noop_modified_ms", statistics.median([run() for _ in range(3)]))
            self.idleWait(100)
        self.attempt(["noop_modified_ms"], noopModified)

        def logicModifiedPerStep():
            count = [0]

            def callback(caller, event):
                count[0] += 1
            tag = redLogic.AddObserver(vtk.vtkCommand.ModifiedEvent, callback)
            try:
                for i in range(20):
                    redLogic.SetSliceOffset(offset0 - 10 + i)
                    slicer.app.processEvents()
                self.pump(2)
            finally:
                redLogic.RemoveObserver(tag)
                redLogic.SetSliceOffset(offset0)
                self.pump(2)
            return count[0] / 20.0

        self.attempt(["slice_logic_modified_per_step"], lambda: self.record("slice_logic_modified_per_step", logicModifiedPerStep()))

        def logicModifiedPerStepForeground():
            ijkToRAS = vtk.vtkMatrix4x4()
            volumeNode.GetIJKToRASMatrix(ijkToRAS)
            foregroundNode = slicer.util.addVolumeFromArray(slicer.util.arrayFromVolume(volumeNode).astype(np.float32), name="BenchForeground")
            foregroundNode.SetIJKToRASMatrix(ijkToRAS)
            slicer.util.setSliceViewerLayers(foreground=foregroundNode, foregroundOpacity=0.5)
            self.pump(4)
            try:
                self.record("slice_logic_modified_per_step_fg", logicModifiedPerStep())
            finally:
                slicer.util.setSliceViewerLayers(foreground=None)
                slicer.mrmlScene.RemoveNode(foregroundNode)
                self.pump(4)
        self.attempt(["slice_logic_modified_per_step_fg"], logicModifiedPerStepForeground)

        def renderCountsOtherViews():
            displayNodes = []
            for name in layoutManager.sliceViewNames():
                displayNode = layoutManager.sliceWidget(name).sliceLogic().GetSliceDisplayNode()
                if displayNode is not None:
                    displayNodes.append((displayNode, displayNode.GetIntersectingSlicesVisibility()))
                    displayNode.SetIntersectingSlicesVisibility(False)
            views = {name: view for name, view in self.allViews().items() if name != "Red"}
            self.pump(4)
            self.idleWait(100)
            counts, removeObservers = self.renderCounters(views)
            try:
                with self.uncappedRendering():
                    for i in range(20):
                        redLogic.SetSliceOffset(offset0 - 10 + i)
                        slicer.app.processEvents()
                    self.idleWait(100)
            finally:
                removeObservers()
                for displayNode, visibility in displayNodes:
                    displayNode.SetIntersectingSlicesVisibility(visibility)
                redLogic.SetSliceOffset(offset0)
                self.pump(2)
            self.record("render_counts_other_views", sum(counts.values()) / 20.0)
            self.details["render_counts_other_views_by_view"] = {name: count / 20.0 for name, count in counts.items()}
        self.attempt(["render_counts_other_views"], renderCountsOtherViews)

        # Measured last: showing the slice in 3D and hiding it again would leave an official build (without the slice edge
        # fix) rendering the 3D view at every slice change, which would inflate the metrics above.
        def scrollShownIn3D():
            def run():
                startTime = time.perf_counter()
                for i in range(100):
                    redLogic.SetSliceOffset(offset0 - 50 + i)
                    slicer.app.processEvents()
                return self.elapsedMs(startTime) / 100
            if layoutManager.threeDViewCount == 0:
                self.skip("scroll_step_3d_ms", "the layout has no 3D view")
                return
            redNode.SetSliceVisible(True)
            self.pump(4)
            self.idleWait(100)
            try:
                run()
                self.record("scroll_step_3d_ms", statistics.median([run() for _ in range(3)]))
            finally:
                redNode.SetSliceVisible(False)
                redLogic.SetSliceOffset(offset0)
                self.pump(4)
                self.idleWait(100)
        self.attempt(["scroll_step_3d_ms"], scrollShownIn3D)
        redNode.SetSliceVisible(sliceVisible)
        self.pump(2)

    def measurePaint256(self):
        import numpy as np
        import slicer
        self.prepareViews()
        rng = np.random.default_rng(0)
        size = 256
        array = (rng.random((size, size, size), dtype=np.float32) * 200).astype(np.int16)
        zz, yy, xx = np.ogrid[:size, :size, :size]
        blob = (xx - 128) ** 2 + (yy - 128) ** 2 + (zz - 128) ** 2 < 30 ** 2
        array[blob] += 600
        volumeNode = self.addVolume(array, "Bench256")
        del array
        layoutManager = slicer.app.layoutManager()
        layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        slicer.util.setSliceViewerLayers(background=volumeNode, foreground=None, label=None, fit=True)
        editor, segmentationNode = self.segmentEditor(volumeNode)
        segmentation = segmentationNode.GetSegmentation()
        lesionId = segmentation.AddEmptySegment("lesion")
        slicer.util.updateSegmentBinaryLabelmapFromArray(blob.astype(np.uint8), segmentationNode, lesionId, volumeNode)
        # Six large slabs that the strokes (on the central axial slice) do not touch
        for i in range(6):
            segmentId = segmentation.AddEmptySegment(f"other{i}")
            mask = np.zeros((size, size, size), np.uint8)
            mask[10 + 40 * i:30 + 40 * i, 30:200, 30:200] = 1
            slicer.util.updateSegmentBinaryLabelmapFromArray(mask, segmentationNode, segmentId, volumeNode)
        del mask
        editor.setCurrentSegmentID(lesionId)
        self.pump(4)
        editor.setActiveEffectByName("Paint")
        effect = editor.activeEffect()
        effect.setCommonParameter("BrushDiameterIsRelative", 0)
        effect.setCommonParameter("BrushAbsoluteDiameter", 8.0)
        self.pump(4)
        startTime = time.perf_counter()
        segmentationNode.CreateClosedSurfaceRepresentation()
        self.pump(2)
        self.details["paint_256_initial_surfaces_ms"] = round(self.elapsedMs(startTime), 1)

        redWidget = layoutManager.sliceWidget("Red")
        redNode = redWidget.mrmlSliceNode()
        redView = redWidget.sliceView()
        interactor = redView.interactorStyle().GetInteractor()
        sliceZ = redWidget.sliceLogic().GetSliceOffset()

        def strokePoints(repetition, count=30):
            # A 12 mm circle centred on the lesion boundary, at a different angle for each stroke, so that every stroke adds voxels
            angle = np.radians(60.0 * repetition)
            center = (128.0 + 30.0 * np.cos(angle), 128.0 + 30.0 * np.sin(angle))
            return [self.rasToXY(redNode, (center[0] + 12.0 * np.cos(2 * np.pi * i / count), center[1] + 12.0 * np.sin(2 * np.pi * i / count), sliceZ)) for i in range(count)]

        def surfaceMTimes():
            mtimes = {}
            for i in range(segmentation.GetNumberOfSegments()):
                representation = segmentation.GetNthSegment(i).GetRepresentation(slicer.vtkSegmentationConverter.GetSegmentationClosedSurfaceRepresentationName())
                mtimes[segmentation.GetNthSegmentID(i)] = representation.GetMTime() if representation else None
            return mtimes

        self.stroke(interactor, strokePoints(0, 10), redView)
        counts, removeObservers = self.renderCounters(self.allViews())

        def hover():
            center = self.rasToXY(redNode, (128.0, 128.0, sliceZ))
            with self.uncappedRendering():
                self.hover(interactor, center, 10)
                for name in counts:
                    counts[name] = 0
                self.record("paint_hover_surfaces_ms", statistics.median([self.hover(interactor, center, 30) for _ in range(3)]))
            self.details["paint_hover_surfaces_render_counts_per_move"] = {name: round(count / 90.0, 2) for name, count in counts.items()}
        self.attempt(["paint_hover_surfaces_ms"], hover)
        removeObservers()

        def strokes():
            releaseTimes = []
            regenerated = []
            for repetition in range(1, 4):
                before = surfaceMTimes()
                releaseTimes.append(self.stroke(interactor, strokePoints(repetition), redView))
                after = surfaceMTimes()
                regenerated.append(sum(1 for segmentId in before if before[segmentId] != after[segmentId]))
            self.record("stroke_release_256_3d_ms", statistics.median(releaseTimes))
            self.record("surfaces_regenerated", statistics.median(regenerated))
            self.details["stroke_release_256_3d_ms_all"] = [round(value, 1) for value in releaseTimes]
            self.details["surfaces_regenerated_all"] = regenerated
            self.details["segments"] = segmentation.GetNumberOfSegments()
        self.attempt(["stroke_release_256_3d_ms", "surfaces_regenerated"], strokes)

        def undo():
            self.record("undo_surfaces_ms", statistics.median([self.timeUntilVisible(redView, editor.undo) for _ in range(3)]))
        self.attempt(["undo_surfaces_ms"], undo)
        editor.setActiveEffectByName("")

    def measurePaintMNI(self):
        import numpy as np
        import slicer
        self.prepareViews()
        rng = np.random.default_rng(0)
        shape = (182, 218, 182)  # MNI 1 mm grid (k, j, i)
        array = (rng.random(shape, dtype=np.float32) * 200).astype(np.int16)
        zz, yy, xx = np.ogrid[:shape[0], :shape[1], :shape[2]]
        array[(xx - 91) ** 2 + (yy - 109) ** 2 + (zz - 91) ** 2 < 15 ** 2] += 600
        volumeNode = self.addVolume(array, "BenchMNI", origin=(-90.0, -126.0, -72.0))
        del array
        layoutManager = slicer.app.layoutManager()
        layoutManager.setLayout(slicer.vtkMRMLLayoutNode.SlicerLayoutFourUpView)
        slicer.util.setSliceViewerLayers(background=volumeNode, foreground=None, label=None, fit=True)
        editor, segmentationNode = self.segmentEditor(volumeNode)
        parameterNode = editor.mrmlSegmentEditorNode()
        segmentation = segmentationNode.GetSegmentation()
        lesionId = segmentation.AddEmptySegment("lesion")
        for i in range(4):
            segmentation.AddEmptySegment(f"other{i}")
        editor.setCurrentSegmentID(lesionId)
        self.pump(4)
        editor.setActiveEffectByName("Paint")
        effect = editor.activeEffect()
        effect.setCommonParameter("BrushDiameterIsRelative", 0)
        effect.setCommonParameter("BrushAbsoluteDiameter", 10.0)
        self.pump(4)
        redWidget = layoutManager.sliceWidget("Red")
        redNode = redWidget.mrmlSliceNode()
        view = redWidget.sliceView()
        interactor = view.interactorStyle().GetInteractor()
        centerX, centerY = view.width // 2, view.height // 2

        def strokePoints(count=40, radius=40):
            return [(centerX, centerY)] + [(int(centerX + radius * np.cos(2 * np.pi * i / count)), int(centerY + radius * np.sin(2 * np.pi * i / count))) for i in range(count)]

        self.stroke(interactor, strokePoints(10), view)

        def plain():
            self.record("stroke_release_mni_ms", statistics.median([self.stroke(interactor, strokePoints(), view) for _ in range(5)]))
        self.attempt(["stroke_release_mni_ms"], plain)

        def intensityMasked():
            parameterNode.SetSourceVolumeIntensityMask(True)
            parameterNode.SetSourceVolumeIntensityMaskRange(400, 2000)
            try:
                self.record("stroke_release_mni_intensity_ms", statistics.median([self.stroke(interactor, strokePoints(), view) for _ in range(5)]))
            finally:
                parameterNode.SetSourceVolumeIntensityMask(False)
        self.attempt(["stroke_release_mni_intensity_ms"], intensityMasked)

        def drawApply():
            editor.setActiveEffectByName("Draw")
            self.pump(2)
            pipeline = editor.activeEffect().self().pipelineForWidget(redWidget)
            times = []
            for repetition in range(3):
                for i in range(20):
                    angle = 2 * np.pi * i / 20
                    pipeline.addPoint(self.xyToRAS(redNode, (centerX + 30 * np.cos(angle) + 10 * repetition, centerY + 30 * np.sin(angle))))
                # As after each interaction event: the polyline that apply() rasterizes is in XY
                pipeline.positionActors()
                times.append(self.timeUntilVisible(view, pipeline.apply))
            self.record("draw_apply_ms", statistics.median(times))
        self.attempt(["draw_apply_ms"], drawApply)
        editor.setActiveEffectByName("")

    def measureSegmentOperations(self):
        import numpy as np
        import slicer
        self.prepareViews()
        rng = np.random.default_rng(0)
        size = 256
        array = (rng.random((size, size, size), dtype=np.float32) * 200).astype(np.int16)
        zz, yy, xx = np.ogrid[:size, :size, :size]
        blob = (xx - 128) ** 2 + (yy - 128) ** 2 + (zz - 128) ** 2 < 10 ** 2
        array[blob] += 600
        volumeNode = self.addVolume(array, "BenchOps256")
        del array
        slicer.util.setSliceViewerLayers(background=volumeNode, foreground=None, label=None, fit=True)
        lesion = np.zeros((size, size, size), np.uint8)
        lesion[blob] = 1
        lesion[60, 60, 60] = 1  # a one-voxel island
        editor, segmentationNode = self.segmentEditor(volumeNode)
        lesionId = segmentationNode.GetSegmentation().AddEmptySegment("lesion")
        editor.setCurrentSegmentID(lesionId)
        redView = slicer.app.layoutManager().sliceWidget("Red").sliceView()

        def reset():
            slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentationNode, lesionId, volumeNode)
            editor.setCurrentSegmentID(lesionId)
            self.pump(2)

        def operation(metric, effectName, parameters):
            def run():
                editor.setActiveEffectByName(effectName)
                effect = editor.activeEffect()
                for name, value in parameters.items():
                    effect.setParameter(name, value)
                times = []
                for repetition in range(3):
                    reset()
                    times.append(self.timeUntilVisible(redView, effect.self().onApply))
                self.record(metric, statistics.median(times))
                self.details[metric + "_all"] = [round(value, 1) for value in times]
            self.attempt([metric], run)

        operation("smoothing_median3_ms", "Smoothing", {"SmoothingMethod": "MEDIAN", "KernelSizeMm": 3.0})
        operation("islands_keep_largest_ms", "Islands", {"Operation": "KEEP_LARGEST_ISLAND"})
        operation("margin_2mm_ms", "Margin", {"MarginSizeMm": 2.0})
        editor.setActiveEffectByName("")

    def measureEffectGuiRefresh(self):
        import numpy as np
        import slicer
        self.prepareViews()
        rng = np.random.default_rng(0)
        shape = (256, 384, 384)  # (k, j, i), 0.6 mm voxels
        spacing = 0.6
        volumeNode = self.addVolume((rng.random(shape, dtype=np.float32) * 100).astype(np.int16), "Bench384", spacing=(spacing, spacing, spacing))
        slicer.util.setSliceViewerLayers(background=volumeNode, foreground=None, label=None, fit=True)
        kk, jj, ii = np.ogrid[:shape[0], :shape[1], :shape[2]]

        def sphere(center, radiusMm):
            return ((kk - center[0]) * spacing) ** 2 + ((jj - center[1]) * spacing) ** 2 + ((ii - center[2]) * spacing) ** 2 <= radiusMm ** 2
        center = (shape[0] // 2, shape[1] // 2, shape[2] // 2)
        lesion = (sphere(center, 10.0) | sphere((center[0] + 30, center[1], center[2] + 40), 2.0)).astype(np.uint8)
        editor, segmentationNode = self.segmentEditor(volumeNode)
        lesionId = segmentationNode.GetSegmentation().AddEmptySegment("lesion")
        slicer.util.updateSegmentBinaryLabelmapFromArray(lesion, segmentationNode, lesionId, volumeNode)
        del lesion
        editor.setCurrentSegmentID(lesionId)
        editor.setActiveEffectByName("Margin")
        self.pump(4)
        effect = editor.activeEffect()
        times = []
        for repetition in range(3):
            startTime = time.perf_counter()
            effect.self().updateGUIFromMRML()
            times.append(self.elapsedMs(startTime))
        self.record("effect_gui_refresh_ms", statistics.median(times))
        editor.setActiveEffectByName("")

    def measureLesionNetworkMapping(self):
        import numpy as np
        moduleDir = os.path.join(SOURCE_DIR, "Modules", "Scripted", "LesionNetworkMapping")
        try:
            import LesionNetworkMappingLib  # noqa: F401
        except ImportError:
            if not os.path.isdir(os.path.join(moduleDir, "LesionNetworkMappingLib")):
                self.skip("lnm_synthetic_s", "LesionNetworkMappingLib is not available")
                return
            sys.path.insert(0, moduleDir)
        from LesionNetworkMappingLib import connectome, nifti
        libraryDir = os.path.dirname(os.path.dirname(os.path.abspath(connectome.__file__)))
        python = shutil.which("PythonSlicer") or sys.executable
        with tempfile.TemporaryDirectory(prefix="StudioBench-lnm-") as tempDir:
            startTime = time.perf_counter()
            meta = connectome.make_synthetic(os.path.join(tempDir, "bench.lnmc"), shape=(30, 36, 30), n_subjects=10, runs=(60, 60), name="StudioBench")
            self.details["lnm_make_synthetic_s"] = round(time.perf_counter() - startTime, 3)
            grid = np.indices(meta["shape"]).reshape(3, -1).T
            world = grid @ np.asarray(meta["affine"])[:3, :3].T + np.asarray(meta["affine"])[:3, 3]
            lesion = (np.linalg.norm(world - np.asarray(meta["blobs"]["A"]["center_world"]), axis=1) <= 4.0).reshape(meta["shape"])
            lesionPath = os.path.join(tempDir, "sub-01_lesion.nii.gz")
            nifti.write_nifti(lesionPath, lesion.astype(np.uint8), np.asarray(meta["affine"]))
            environment = dict(os.environ)
            environment["PYTHONPATH"] = libraryDir + os.pathsep + environment.get("PYTHONPATH", "")
            command = [python, "-m", "LesionNetworkMappingLib.engine", "--bundle", meta["path"], "--lesion", lesionPath, "--out", os.path.join(tempDir, "out"), "--threads", "2"]
            startTime = time.perf_counter()
            process = subprocess.run(command, capture_output=True, text=True, env=environment, cwd=tempDir, timeout=600, check=False)
            elapsed = time.perf_counter() - startTime
            events = []
            for line in process.stdout.splitlines():
                try:
                    events.append(json.loads(line)["event"])
                except (ValueError, KeyError, TypeError):
                    pass
            if process.returncode != 0 or not events or events[-1] != "done":
                raise RuntimeError(f"engine CLI failed (exit code {process.returncode}, events {events}): {process.stderr[-2000:]}")
            self.record("lnm_synthetic_s", elapsed)
            self.details["lnm_python"] = python

    # -- driver

    def widgetMetrics(self):
        import qt
        import slicer
        style = slicer.app.style()
        font = slicer.app.font()
        metrics = {
            "style": style.className(),
            "font": [font.family(), font.pointSizeF()],
            "testing": bool(slicer.app.testingEnabled()),
        }
        for name in ("QPushButton", "QToolButton"):
            for text in ("", "Apply"):
                widget = getattr(qt, name)()
                widget.setText(text)
                hint = widget.sizeHint  # a property in PythonQt
                metrics[f"{name}({text!r}).sizeHint"] = [hint.width(), hint.height()]
        for name in ("PM_LayoutHorizontalSpacing", "PM_LayoutVerticalSpacing", "PM_LayoutLeftMargin", "PM_LayoutTopMargin",
                     "PM_ButtonIconSize", "PM_ToolBarIconSize", "PM_SmallIconSize", "PM_DefaultFrameWidth"):
            metrics[name] = style.pixelMetric(getattr(qt.QStyle, name))
        return metrics

    def run(self, selectedGroups):
        import qt
        import slicer
        mainWindow = slicer.util.mainWindow()
        if mainWindow is not None:
            mainWindow.showMaximized()
            mainWindow.resize(1920, 1080)
        # Let startup settle, including work that the application defers to idle time
        self.pump(10)
        self.idleWait(1000)
        try:
            self.details["widget_metrics"] = self.widgetMetrics()
        except Exception:
            self.details["widget_metrics"] = traceback.format_exc()[-1000:]
        shell = self.studioShell()
        self.details["studio_active"] = shell is not None
        for group, metricNames, methodName in GROUPS:
            if group not in selectedGroups:
                continue
            print(f"BENCH group {group}", flush=True)
            startTime = time.perf_counter()
            self.attempt(metricNames, getattr(self, methodName))
            self.details.setdefault("group_seconds", {})[group] = round(time.perf_counter() - startTime, 1)
            try:
                if slicer.util.mainWindow() is not None:
                    slicer.util.selectModule("Data")
                self.clearScene()
            except Exception:
                traceback.print_exc()
        self.details["main_window_size"] = [mainWindow.width, mainWindow.height] if mainWindow is not None else None
        self.details["qt_version"] = qt.qVersion()


def resolveSelection(only):
    """Return (groups, metrics) selected by --only (metrics is None when everything is selected)."""
    if not only:
        return {group for group, _metrics, _method in GROUPS}, None
    groups = set()
    metrics = set()
    for name in (item.strip() for item in only.split(",") if item.strip()):
        matched = False
        for group, groupMetrics, _method in GROUPS:
            if name == group:
                groups.add(group)
                metrics.update(groupMetrics)
                matched = True
            elif name in groupMetrics:
                groups.add(group)
                metrics.add(name)
                matched = True
        if not matched:
            raise ValueError(f"unknown metric or group '{name}' (see --list)")
    return groups, metrics


def runInSlicer(argv):
    import slicer
    exitCode = 2
    try:
        args = parseArguments(argv, inSlicer=True)
        if args.list:
            listMeasurements()
            exitCode = 0
            return
        groups, selectedMetrics = resolveSelection(args.only)
        try:
            loadCompareResult(args)  # before measuring, which takes minutes
        except (OSError, ValueError) as error:
            print(f"error: --compare: {error}", file=sys.stderr)
            return
        meta = baseMeta([SCRIPT_PATH] + list(argv))
        bench = InSlicerBench(args)
        startTime = time.perf_counter()
        bench.run(groups)
        if selectedMetrics is not None:
            bench.metrics = {name: value for name, value in bench.metrics.items() if name in selectedMetrics}
            bench.errors = {name: value for name, value in bench.errors.items() if name in selectedMetrics}
            bench.details["skipped"] = {name: value for name, value in bench.details["skipped"].items() if name in selectedMetrics}
        meta.update({
            "mode": "in-slicer",
            "build": {
                "version": slicer.app.applicationVersion,
                "revision": slicer.app.revision,
                "home": slicer.app.slicerHome,
                "main_window": slicer.util.mainWindow() is not None,
            },
            "duration_s": round(time.perf_counter() - startTime, 1),
            "loadavg_end": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
        })
        result = {"metrics": bench.metrics, "errors": bench.errors, "details": bench.details, "meta": meta}
        writeResult(args.out, result)
        print(f"{sum(1 for value in bench.metrics.values() if value is not None)} non-null metrics, {len(bench.errors)} errors", flush=True)
        exitCode = evaluate(result, args)
    except SystemExit as exception:
        exitCode = exception.code if isinstance(exception.code, int) else 2
    except Exception:
        traceback.print_exc()
        exitCode = 2
    finally:
        sys.stdout.flush()
        slicer.app.exit(exitCode)


#
# System-Python modes
#

def parseStartupReport(text):
    """Return {metric: ms} from the --report-startup-timing output."""
    phases = {}
    inReport = False
    for line in text.splitlines():
        if line.startswith("Startup timing report"):
            inReport = True
            continue
        if not inReport:
            continue
        match = re.match(r"^  Total, from the creation of the process: (\d+(?:\.\d+)?) ms", line)
        if match:
            phases["startup_total_ms"] = float(match.group(1))
            break
        # Phase lines are indented by two spaces; the per-module lines below them by four
        match = re.match(r"^  (\S[^:]*): (\d+(?:\.\d+)?) ms$", line)
        if match:
            label = match.group(1)
            key = STARTUP_PHASES.get(label) or "startup_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") + "_ms"
            phases[key] = float(match.group(2))
    return phases


def parseStartupProbe(text):
    """Return {metric: ms} from the output of STARTUP_PROBE_SCRIPT (empty when Studio was not applied)."""
    match = re.search(r"^StudioBench startup probe: studio_apply_ms=(\d+(?:\.\d+)?)$", text, re.MULTILINE)
    return {"startup_studio_apply_ms": float(match.group(1))} if match else {}


def runStartup(args):
    probeDirectory = tempfile.mkdtemp(prefix="StudioBench-")
    try:
        probePath = os.path.join(probeDirectory, "StudioBenchStartupProbe.py")
        with open(probePath, "w") as f:
            f.write(STARTUP_PROBE_SCRIPT)
        return runStartupWithProbe(args, probePath)
    finally:
        shutil.rmtree(probeDirectory, ignore_errors=True)


def runStartupWithProbe(args, probePath):
    command = [args.slicer, "--no-splash", "--report-startup-timing", "--exit-after-startup", "--python-script", probePath] + args.slicer_args.split()
    if not args.no_xvfb:
        command = ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24"] + command
    environment = dict(os.environ)
    if args.home:
        os.makedirs(args.home, exist_ok=True)
        environment["HOME"] = args.home
    logPrefix = os.path.splitext(args.out)[0] if args.out else os.path.join(tempfile.gettempdir(), "StudioBench-startup")
    runs = []
    errors = {}
    for run in range(args.runs):
        startTime = time.perf_counter()
        process = subprocess.run(command, capture_output=True, text=True, env=environment, timeout=args.timeout, check=False)
        wall = (time.perf_counter() - startTime) * 1000.0
        output = process.stdout + process.stderr
        logPath = f"{logPrefix}_run{run + 1}.log"
        os.makedirs(os.path.dirname(os.path.abspath(logPath)), exist_ok=True)
        with open(logPath, "w") as f:
            f.write(output)
        phases = parseStartupReport(output)
        phases["startup_wall_ms"] = wall
        phases.update(parseStartupProbe(output))
        if process.returncode != 0 or "startup_total_ms" not in phases:
            errors[f"run{run + 1}"] = f"exit code {process.returncode}, no timing report found; see {logPath}"
        runs.append(phases)
        print(f"run {run + 1}: " + ", ".join(f"{key}={value:.0f}" for key, value in sorted(phases.items())), flush=True)
    names = sorted({name for phases in runs for name in phases})
    metrics = {}
    for name in names:
        values = [phases[name] for phases in runs if name in phases]
        metrics[name] = round(statistics.median(values), 1) if values else None
    if "startup_total_ms" not in metrics:
        metrics["startup_total_ms"] = None
        errors["startup_total_ms"] = "no run produced a startup timing report"
    meta = baseMeta(sys.argv)
    meta.update({"mode": "startup", "command": command, "runs": args.runs, "home": args.home, "loadavg_end": list(os.getloadavg())})
    return {"metrics": metrics, "errors": errors, "details": {"runs": runs}, "meta": meta}


def mergeResults(paths):
    """Per-metric median of several result files (null only when no run produced a value).

    A metric that failed in any run keeps an entry in "errors" even when other runs produced a value,
    so that a measurement that fails intermittently still fails the gate.
    """
    results = []
    for path in paths:
        with open(path) as f:
            results.append(json.load(f))
    metrics = {}
    errors = {}
    for name in sorted({name for result in results for name in result.get("metrics", {})}):
        values = [result["metrics"][name] for result in results if result.get("metrics", {}).get(name) is not None]
        metrics[name] = round(statistics.median(values), 3) if values else None
        failedRuns = [(path, result) for path, result in zip(paths, results, strict=True)
                      if name in result.get("errors", {}) or (name in result.get("metrics", {}) and result["metrics"][name] is None)]
        if failedRuns:
            errors[name] = (f"failed in {len(failedRuns)} of {len(results)} runs\n" + "\n".join(
                f"{os.path.basename(path)}: {result.get('errors', {}).get(name, 'null')}" for path, result in failedRuns))[-3000:]
    for path, result in zip(paths, results, strict=True):
        for name, message in result.get("errors", {}).items():
            if name not in metrics:  # errors that are not about a metric, for example a failed startup run
                errors[f"{os.path.basename(path)}:{name}"] = message
    details = dict(results[0].get("details", {}))
    details["skipped"] = {name: reason for result in results for name, reason in result.get("details", {}).get("skipped", {}).items()
                          if name not in metrics}
    details["merged_from"] = [os.path.abspath(path) for path in paths]
    details["merged_metrics"] = [result.get("metrics", {}) for result in results]
    meta = baseMeta(sys.argv)
    meta.update({"mode": "merge", "inputs": [result.get("meta", {}) for result in results]})
    return {"metrics": metrics, "errors": errors, "details": details, "meta": meta}


def runSystemPython(argv):
    try:
        args = parseArguments(argv, inSlicer=False)
    except SystemExit as exception:
        return exception.code if isinstance(exception.code, int) else 2
    if args.list:
        listMeasurements()
        return 0
    loadCompareResult(args)  # before measuring; raises OSError or ValueError (exit code 2)
    if args.input:
        with open(args.input) as f:
            result = json.load(f)
    elif args.merge:
        if not args.out:
            print("--out is required with --merge", file=sys.stderr)
            return 2
        result = mergeResults(args.merge)
        writeResult(args.out, result)
    elif args.startup:
        if not args.out:
            print("--out is required with --startup", file=sys.stderr)
            return 2
        result = runStartup(args)
        writeResult(args.out, result)
    else:
        print("Outside Slicer use --startup, --merge or --input; to measure the application run this script with "
              "'Slicer --python-script StudioBench.py -- --out F.json'.", file=sys.stderr)
        return 2
    return evaluate(result, args)


def inSlicer():
    try:
        import slicer
    except ImportError:
        return False
    return getattr(slicer, "app", None) is not None


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments and arguments[0] == "--":
        arguments = arguments[1:]
    if inSlicer():
        runInSlicer(arguments)
    else:
        try:
            sys.exit(runSystemPython(arguments))
        except (OSError, ValueError) as error:  # for example a missing or invalid JSON file
            print(f"error: {error}", file=sys.stderr)
            sys.exit(2)

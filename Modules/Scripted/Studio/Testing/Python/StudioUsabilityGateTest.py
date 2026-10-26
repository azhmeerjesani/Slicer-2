r"""Fast guard of the usability gate (U-05): the novice and find-tools walkthroughs with synthetic input, state assertions only.

Run with the main window and testing enabled:

  Slicer --no-splash --testing --python-code "import slicer.testing; slicer.testing.runUnitTest(['<this dir>'], 'StudioUsabilityGateTest'); slicer.app.exit(0)"

The full gate (all tasks, real X input, action counts and targets) is ``Utilities/Scripts/StudioUsabilityGate.py``. This test runs
two of its tasks through the same driver (``StudioUsabilityGate.harness`` and ``tasks``) with Qt events as input, so that every work
package keeps the state assertions of USABILITY section 16 that guard the quick wins (tool kept on return, stable inspector width,
Tab never toggles the inspector, the Esc ladder, Classic -> Studio -> Draw, edge letters, saved lesion grid, ...). It checks
the assertions that must pass from wave 1, never the action counts, and fails on an exception of a task.
"""

import os
import shutil
import sys
import tempfile

import qt

import slicer
from slicer import ScriptedLoadableModule

#: <source>/Modules/Scripted/Studio/Testing/Python -> <source>/Utilities/Scripts
_SCRIPTS_DIRECTORY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), *([os.pardir] * 5), "Utilities", "Scripts"))


def _exec(loop):
    return (getattr(loop, "exec", None) or loop.exec_)()


class StudioUsabilityGateTest(ScriptedLoadableModule.ScriptedLoadableModuleTest):
    """The usability gate with synthetic input (needs the main window)."""

    def setUp(self):
        slicer.mrmlScene.Clear(0)
        self.mainWindow = slicer.util.mainWindow()
        self.initialGeometry = self.mainWindow.saveGeometry() if self.mainWindow is not None else None
        self.directory = tempfile.mkdtemp(prefix="studio-gate-")
        if _SCRIPTS_DIRECTORY not in sys.path:
            sys.path.insert(0, _SCRIPTS_DIRECTORY)

    def tearDown(self):
        from StudioLib import shell

        live = shell._shell
        if live is not None:
            live.dispose()
        interactionNode = slicer.app.applicationLogic().GetInteractionNode()
        if interactionNode is not None:
            interactionNode.SwitchToViewTransformMode()
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            layoutManager.removeAllMaximizedViewNodes()
        if self.initialGeometry is not None:
            self.mainWindow.restoreGeometry(self.initialGeometry)
        slicer.mrmlScene.Clear(0)
        shutil.rmtree(self.directory, ignore_errors=True)
        slicer.app.processEvents()

    def runTest(self):
        self.setUp()
        try:
            self.test_targets_reachable()
            self.test_clipped_by_outside_viewport()
            self.test_state_assertions()
        finally:
            self.tearDown()

    def _runTasks(self, names, timeoutSeconds=100):
        from StudioUsabilityGate import fixtures, harness, tasks

        self.assertIsNotNone(self.mainWindow, "the usability gate needs the main window")
        gate = harness.Gate(self.directory, inputMode="synthetic", wave=1, quiet=True, logName="test")
        gate.config.update({"fast": True, "viaRc": False})
        gate.fixtures = fixtures.makeFixtures(os.path.join(self.directory, "data"))
        outcome = {}
        loop = qt.QEventLoop()

        def done(results, errors):
            outcome["results"], outcome["errors"] = results, errors
            loop.quit()

        scheduler = tasks.runSpecs(gate, [tasks.taskSpec(name) for name in names], 1, done)
        qt.QTimer.singleShot(200, scheduler.start)
        timer = qt.QTimer()
        timer.setSingleShot(True)
        timer.connect("timeout()", loop.quit)
        timer.start(timeoutSeconds * 1000)
        _exec(loop)
        timer.stop()
        self.assertIn("results", outcome, f"the tasks {names} did not finish within {timeoutSeconds} s")
        return outcome["results"]

    def test_targets_reachable(self):
        """The find-tools total target leaves room for the cheapest T7 (Ctrl+K and Esc per query) and T10 on top of the scored target."""
        from StudioUsabilityGate import harness, tasks

        targets = harness.loadTargets()
        cheapestExploration = 2 * len(tasks.T7_QUERIES) + tasks.T10_ACTIONS
        for wave in (2, 3, 4):
            total = targets["find-tools.total"][f"target_w{wave}"]
            scored = targets["find-tools"][f"target_w{wave}"]
            self.assertLessEqual(scored + cheapestExploration, total, f"wave {wave}: scored {scored} + the cheapest T7 and T10 {cheapestExploration} exceeds the total target {total}")

    def test_targets_later_wave(self):
        """A wave without its own target keeps the latest earlier one; wave 5 lowers only the case target (L-45: <= 35)."""
        from StudioUsabilityGate import harness

        targets = harness.loadTargets()
        self.assertEqual(harness.targetFor(targets, "case", 5), 35)
        self.assertEqual(harness.targetFor(targets, "case", 4), 45)
        for key in ("novice", "nav", "find-tools", "lnm", "lnm.B"):
            self.assertEqual(harness.targetFor(targets, key, 5), targets[key]["target_w4"], key)
        self.assertIsNone(harness.targetFor(targets, "lnm.B", 2))

    def test_clipped_by_outside_viewport(self):
        """Gate.clippedBy on children of a scroll area outside its viewport (header, scroll bar) and inside it.

        ``QWidget::mapTo`` to a widget that is not an ancestor crashes Qt 5 release builds: a header or scroll bar of a table must
        not be mapped to the table's viewport.
        """
        from StudioUsabilityGate import harness

        table = qt.QTableWidget(40, 3)
        try:
            table.resize(300, 200)
            table.show()
            slicer.app.processEvents()
            self.assertIsNone(harness.Gate.clippedBy(table.horizontalHeader(), 5, 5))
            self.assertIsNone(harness.Gate.clippedBy(table.verticalScrollBar(), 2, 2))
            # A widget inside the viewport: its visible point is shown, a point far below the viewport is clipped
            label = qt.QLabel("cell", table.viewport())
            label.setGeometry(0, 0, 40, 20)
            label.show()
            self.assertIsNone(harness.Gate.clippedBy(label, 2, 2))
            self.assertIsNotNone(harness.Gate.clippedBy(label, 2, 2000))
        finally:
            table.hide()
            table.deleteLater()
            slicer.app.processEvents()

    def test_state_assertions(self):
        """Novice and find-tools: no exception, and no state assertion that must pass from wave 1 fails."""
        results = self._runTasks(["novice", "find-tools"], timeoutSeconds=110)
        for name, result in results.items():
            self.assertIsNone(result["error"], f"{name}: {result['error']}")
            self.assertEqual(result["failedAssertions"], [], f"{name}: {result['failedAssertions']}")
            self.assertGreater(result["totalActions"], 10, f"{name} acted {result['totalActions']} times")
        self.assertGreater(len(results["novice"]["assertions"]), 3)
        self.assertIn("effect-after-return", results["find-tools"]["assertions"])

"""Preflight card of the Map module (order 30): what a run will analyse and what is wrong, before any compute (L-30).

- **Checks.** ``LesionNetworkMappingLib.preflight`` (L-17) runs as a background command line (``--strict``: a lesion that
  is missing or unreadable blocks the run) whenever the study, the connectome, the threshold rule, the planned N or the
  overrides change, and from ``ctx.beforeCompute`` when the last report is older than :data:`FRESH_S` seconds or
  describes other settings (then in the process: the checks read headers only). Compute is refused while a blocking
  check exists (``ctx.setComputeEnabled`` and the hook), so no map is started on an invalid run. The rows show the
  message of each check with its fixes; a green report collapses to "All N checks passed".
- **Fixes.** Show in the Library (filtered to the subjects), switch the rule to FWE or to an effect-size rule,
  re-measure the lesion metrics (a worker thread, then the study inbox), offer the template-family transform (the
  Library's warp), scroll to the data use terms, and "Include anyway" for review exclusions (recorded in the report's
  ``settings.overrides`` and in ``ctx.preflightOverrides``).
- **Report.** The report of a run that is started is saved to ``derivatives/qc/preflight-<UTC>.json`` (no local paths).
- **Output assertions.** ``ctx.afterRun`` holds :meth:`PreflightCard.assertRun`: every engine, cohort maps, group and GLM
  job is checked with ``preflight.assert_run_outputs`` (finite, valid, correctly headed outputs of the requested N). A
  failure fails the job, shows "Run failed: <reason>" in the card and turns the Studio status chip red.

The card is shown for the Cohort seed (a study); the checks are skipped for the other seed modes. Works in Classic.
"""

import json
import logging
import os
import subprocess
import threading
import time

import qt
import slicer
from slicer.i18n import tr as _

from . import ORDER_PREFLIGHT, jobrunner, widgets

#: Kinds of ``ctx.submit`` jobs whose outputs are asserted after the run, and the record each one writes
ASSERTED_KINDS = {"engine": "run.json", "cohortmaps": "maps.tsv", "glm": "glm.json", "slnm": "slnm.json", "cohort": "cohort.json"}
#: The report is reused by the compute hook while it is younger than this (s) and describes the same settings
FRESH_S = 5.0
#: Quiet time after a change before the check runs (ms)
RECHECK_MS = 400
#: Cohort maps runs with more maps than this have the values of a sample of this many maps checked (the hook runs on the GUI thread)
MAX_VALUE_CHECKS = 30
#: Longest the background command line may take (s)
JOB_TIMEOUT_S = 300
#: Hashing of lesion files is budgeted per preflight: a report that defers some is repeated this many times at most
MAX_DEFERRED_REPEATS = 3
#: What a green report says it checked (N of "All N checks passed"; "design" only with a design)
CHECKED = (
    _("lesion files are readable and 3D"),
    _("lesions are not empty or smaller than a voxel"),
    _("lesion metrics are current"),
    _("lesions are in the connectome's template family"),
    _("review and normalization status"),
    _("no duplicate subjects or lesion files"),
    _("data use terms accepted"),
    _("enough disk space"),
    _("threshold rule fits N and the connectome"),
)
DESIGN_CHECK = _("design matrix is estimable")
#: Fix actions that the review override can cover (the cohort lesion list takes them in)
REVIEW_OVERRIDES = ("unreviewed", "auto_unreviewed")
LEVEL_OF_CHECK = {"block": "danger", "warn": "warn", "info": "neutral"}
RULE_EFFECT_SIZE = {"kind": "top_pct"}


def _widget():
    return getattr(slicer.modules, "LesionNetworkMappingWidget", None)


def _bannerLevel(check):
    return LEVEL_OF_CHECK.get(check.get("level"), "neutral")


class PreflightCard:
    """Controller of the Preflight card (``widget`` is a collapsible card)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.report = None  # the latest report (a dict of preflight.run_preflight)
        self.reportAt = 0.0
        self.reportSignature = None
        self.savedPath = None
        self.overrides = set()  # warn-level exclusions included anyway
        self.probabilistic = False
        self.showNotes = False
        self.rows = []
        self.runFailure = None
        self._state = None
        self._failedJobs = {}  # job id -> reason of the output assertions that failed it
        self._runningSignature = None
        self._worker = None
        self._workerResult = None
        self._dirty = False
        self._deferredRepeats = 0
        self._measure = None
        self._observedNode = None
        self._observer = None
        self._termsBox = None

        self.widget = widgets.card(_("Preflight"), objectName="LNMPreflightCard", collapsed=True)
        layout = qt.QVBoxLayout(self.widget)
        headRow = qt.QHBoxLayout()
        self.chip = widgets.chip(_("Not checked"), "neutral")
        self.chip.setObjectName("LNMPreflightChip")
        headRow.addWidget(self.chip, 0, qt.Qt.AlignTop)
        self.summaryLabel = self._label("LNMPreflightSummary")
        headRow.addWidget(self.summaryLabel, 1)
        self.recheckButton = qt.QPushButton(_("Check again"))
        self.recheckButton.setObjectName("LNMPreflightRecheckButton")
        self.recheckButton.setProperty("slicerRole", "ghost")
        self.recheckButton.setToolTip(_("Run the preflight checks again now"))
        self.recheckButton.connect("clicked()", lambda: self.recheck(force=True))
        headRow.addWidget(self.recheckButton, 0, qt.Qt.AlignTop)
        layout.addLayout(headRow)
        self.rowsLayout = qt.QVBoxLayout()
        self.rowsLayout.setSpacing(6)
        layout.addLayout(self.rowsLayout)
        self.notesButton = qt.QPushButton("")
        self.notesButton.setObjectName("LNMPreflightNotesButton")
        self.notesButton.setProperty("slicerRole", "ghost")
        self.notesButton.connect("clicked()", self.toggleNotes)
        self.notesButton.hide()
        layout.addWidget(self.notesButton)
        self.progressLabel = self._label("LNMPreflightProgress", role="secondaryText")
        self.progressLabel.hide()
        layout.addWidget(self.progressLabel)
        self.failureHost = qt.QVBoxLayout()
        layout.addLayout(self.failureHost)

        self.timer = qt.QTimer()
        self.timer.setSingleShot(True)
        self.timer.setInterval(RECHECK_MS)
        self.timer.connect("timeout()", self.recheck)
        self.poller = qt.QTimer()
        self.poller.setInterval(100)
        self.poller.connect("timeout()", self._poll)
        ctx.beforeCompute.append(self.checkBeforeCompute)
        ctx.afterRun.append(self.assertRun)
        ctx.runner.jobFinished.append(self._onJobFinished)
        for name in ("studyChanged", "bundleChanged", "ruleChanged", "seedChanged"):
            getattr(ctx, name).append(self.schedule)
        self._observeParameterNode()
        self._observeTerms()
        # The Map widget sets the parameter node after it has built the cards: observe it once the widget is set up
        qt.QTimer.singleShot(0, self._lateInit)
        self.schedule()

    def _alive(self):
        """False once the card's widget was destroyed (a module widget that is torn down right after it was set up)."""
        try:
            self.widget.objectName
        except (ValueError, RuntimeError):
            return False
        return True

    def _lateInit(self):
        if not self._alive():
            return
        self._observeParameterNode()
        self._observeTerms()
        self._installCohortShim()
        self.schedule()

    def _installCohortShim(self):
        """Pass the review overrides to the cohort maps job (``--include-unreviewed``), so that what is mapped is what was checked.

        ``LesionNetworkMappingLogic.startCohortMaps`` builds the job's arguments itself and does not know the overrides
        (``ctx.preflightOverrides``): this wraps it on the logic object for the time of the call. Delete it once the logic
        reads ``ctx.preflightOverrides`` itself (the flag is only added when it is not in the arguments yet).
        """
        logic = self.ctx.logic
        if logic is None or getattr(logic, "_preflightShim", None) is not None or not hasattr(logic, "startCohortMaps"):
            return
        original = logic.startCohortMaps
        card = self

        def startCohortMaps(config, onEvent=None, onDone=None, context=None):
            if not (card.overrides & set(REVIEW_OVERRIDES)):
                return original(config, onEvent, onDone, context)
            runner = context.runner if context is not None else jobrunner.instance()
            submit = runner.submit

            def submitWithOverrides(spec, context=None):
                if "--include-unreviewed" not in spec.args:
                    spec.args.append("--include-unreviewed")
                return submit(spec, context=context)

            runner.submit = submitWithOverrides
            try:
                return original(config, onEvent, onDone, context)
            finally:
                del runner.submit  # back to the runner's own method

        logic.startCohortMaps = startCohortMaps
        logic._preflightShim = original

    def _label(self, objectName, role=None):
        label = qt.QLabel("")
        label.setObjectName(objectName)
        label.setWordWrap(True)
        label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        if role:
            label.setProperty("slicerRole", role)
        return label

    #
    # Card protocol
    #

    def onStudyChanged(self):
        self.schedule()

    def onCohortChanged(self):
        # refreshCohort() rebuilt the cohort list without the review overrides: put them back before checking
        self._syncCohortOverrides()
        self.schedule()

    def onRunSettingsChanged(self):
        self.schedule()

    def cleanup(self):
        for name in ("studyChanged", "bundleChanged", "ruleChanged", "seedChanged"):
            callbacks = getattr(self.ctx, name, [])
            if self.schedule in callbacks:
                callbacks.remove(self.schedule)
        for hooks, hook in ((self.ctx.beforeCompute, self.checkBeforeCompute), (self.ctx.afterRun, self.assertRun), (self.ctx.runner.jobFinished, self._onJobFinished)):
            if hook in hooks:
                hooks.remove(hook)
        if self._observedNode is not None and self._observer is not None:
            self._observedNode.RemoveObserver(self._observer)
        self._observedNode = None
        self.timer.stop()
        self.poller.stop()
        logic = self.ctx.logic
        if logic is not None and getattr(logic, "_preflightShim", None) is not None:
            logic.startCohortMaps = logic._preflightShim
            del logic._preflightShim
        self.ctx.setComputeEnabled(True, key="preflight")

    def _observeParameterNode(self):
        """Seed mode and planned N are parameters of the module: a change is noticed here (the check itself is debounced)."""
        wrapper = self.ctx.parameterNode
        node = getattr(wrapper, "parameterNode", wrapper)
        if node is None or node is self._observedNode or not hasattr(node, "AddObserver"):
            return
        import vtk

        self._observedNode = node
        self._observer = node.AddObserver(vtk.vtkCommand.ModifiedEvent, lambda caller, event: self.schedule())

    def _observeTerms(self):
        """The Connectome card's "I accept" box changes the terms check (it is built before this card)."""
        widget = _widget()
        box = getattr(widget, "acceptTermsCheckBox", None)
        if box is not None and box is not self._termsBox:
            self._termsBox = box
            box.connect("toggled(bool)", lambda checked: self.schedule())

    #
    # Inputs
    #

    def _mode(self):
        node = self.ctx.parameterNode
        try:
            return node.seedMode or "lesion" if node is not None else "lesion"
        except AttributeError:
            return "lesion"

    def active(self):
        """True when this card gates the compute: the Cohort seed with an open study and a connectome."""
        return self._mode() == "cohort" and self.ctx.study is not None and self.ctx.bundle is not None

    def _rule(self):
        widget = _widget()
        try:
            return widget.currentRule() if widget is not None else self.ctx.rule
        except Exception:
            return self.ctx.rule

    def _plannedN(self):
        widget = _widget()
        try:
            return int(widget.plannedN()) if widget is not None else None
        except Exception:
            return None

    def _termsAccepted(self):
        bundle = self.ctx.bundle
        logic = self.ctx.logic
        try:
            return bool(logic.termsAccepted(bundle)) if logic is not None and bundle is not None else False
        except Exception:
            return False

    def _designPath(self):
        path = getattr(self.ctx, "designPath", None)
        return os.fspath(path) if path and os.path.isfile(path) else None

    def _participantsStamp(self):
        study = self.ctx.study
        try:
            stat = os.stat(os.path.join(study.path, "participants.tsv"))
            return (stat.st_mtime_ns, stat.st_size)
        except (AttributeError, OSError):
            return None

    def signature(self):
        """What the report describes: a change of any of it makes the last report stale."""
        from LesionNetworkMappingLib import thresholds

        try:
            rule = thresholds.rule_to_json(self._rule()) if self._rule() else ""
        except (ValueError, TypeError):
            rule = ""
        study, bundle = self.ctx.study, self.ctx.bundle
        return (getattr(study, "path", None), self._participantsStamp(), getattr(bundle, "path", None), rule, self._plannedN(), self._termsAccepted(), tuple(sorted(self.overrides)),
                self.probabilistic, self._designPath())

    def _kwargs(self):
        """Arguments of ``preflight.run_preflight`` (and, as command line, of the background check)."""
        return {"accepted_terms": self._termsAccepted(), "probabilistic": self.probabilistic, "strict": True, "overrides": sorted(self.overrides), "n_subjects": self._plannedN(), "recipe_rule": self._rule(), "design_spec": self._designPath()}

    def arguments(self, outPath):
        from LesionNetworkMappingLib import thresholds

        kwargs = self._kwargs()
        args = [jobrunner.pythonExecutable(), "-m", "LesionNetworkMappingLib.preflight", "--study", self.ctx.study.path, "--bundle", self.ctx.bundle.path, "--strict", "--json", outPath, "--threads", "1"]
        if kwargs["recipe_rule"]:
            args += ["--rule", thresholds.rule_to_json(kwargs["recipe_rule"])]
        if kwargs["accepted_terms"]:
            args.append("--accepted-terms")
        if kwargs["probabilistic"]:
            args.append("--probabilistic")
        if kwargs["n_subjects"]:
            args += ["--subjects", str(kwargs["n_subjects"])]
        if kwargs["design_spec"]:
            args += ["--design", kwargs["design_spec"]]
        for check in kwargs["overrides"]:
            args += ["--include-anyway", check]
        return args

    #
    # Running the checks
    #

    def schedule(self, *args):
        """Check again after a quiet time (every change of the study, connectome, rule, N or terms ends up here)."""
        if not self._alive():
            return
        self._observeParameterNode()
        if not self.active():
            self.timer.stop()
            self._clear()
            return
        if self.report is not None and self.reportSignature == self.signature():
            return
        self.timer.start()

    def recheck(self, force=False):
        """Start the background check unless the report is current (``force``: always)."""
        if not self.active():
            self._clear()
            return
        signature = self.signature()
        if self._worker is not None:
            self._dirty = True  # repeated when the running check ends
            return
        if not force and self.report is not None and self.reportSignature == signature:
            return
        self._startWorker(signature)

    def _startWorker(self, signature):
        study = self.ctx.study
        folder = os.path.join(study.path, ".studio", "cache")
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            folder = os.path.join(study.path, ".studio")
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:  # a read-only study
                self.progressLabel.setText(_("The preflight check failed: {reason}").format(reason=exc))
                self.progressLabel.show()
                widgets.setChipLevel(self.chip, "warn", _("Not checked"))
                return
        outPath = os.path.join(folder, "preflight-live.json")
        try:
            os.remove(outPath)
        except OSError:
            pass
        try:
            args = self.arguments(outPath)
        except Exception:
            logging.exception("Lesion network mapping: the preflight check could not be prepared")
            return
        self._runningSignature = signature
        self._workerResult = None
        self._dirty = False
        self.progressLabel.setText(_("Checking the study…"))
        self.progressLabel.show()
        widgets.setChipLevel(self.chip, "busy", _("Checking…"))
        env = jobrunner.instance().environment(jobrunner.JobSpec("LesionNetworkMapping"), 1)

        def work():
            result = {"returncode": None, "error": None}
            try:
                process = subprocess.run(
                    args, capture_output=True, text=True, timeout=JOB_TIMEOUT_S, cwd=jobrunner.moduleDir(), env=dict(os.environ, **env),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
                result["returncode"] = process.returncode
                result["tail"] = (process.stdout or "")[-2000:] + (process.stderr or "")[-2000:]
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"
            if result["error"] is None and os.path.isfile(outPath):
                try:
                    with open(outPath, encoding="utf-8") as f:
                        result["report"] = json.load(f)
                except (OSError, ValueError) as exc:
                    result["error"] = f"the report cannot be read: {exc}"
            elif result["error"] is None:
                lines = (result.get("tail") or "").strip().splitlines()
                result["error"] = "the check did not write a report" + (f": {lines[-1]}" if lines else "")
            self._workerResult = result  # read by the poller on the main thread

        self._worker = threading.Thread(target=work, name="lnm-preflight", daemon=True)
        self._worker.start()
        self.poller.start()

    def _poll(self):
        result = self._workerResult
        if result is None:
            return
        self.poller.stop()
        self._worker = None
        self._workerResult = None
        study = self.ctx.study
        if study is not None:
            try:
                os.remove(os.path.join(study.path, ".studio", "cache", "preflight-live.json"))
            except OSError:
                pass
        report = result.get("report")
        if report is None:
            self.progressLabel.setText(_("The preflight check failed: {reason}").format(reason=result.get("error") or "?"))
            widgets.setChipLevel(self.chip, "warn", _("Not checked"))
            logging.warning(f"Lesion network mapping: the preflight check failed: {result.get('error')}")
        else:
            self._takeReport(report, self._runningSignature)
        if self._dirty:
            self._dirty = False
            self.schedule()

    def _takeReport(self, report, signature):
        self.report = report
        self.reportAt = time.monotonic()
        self.reportSignature = signature
        self.ctx.preflightReport = report
        self.ctx.preflightOverrides = sorted(self.overrides)
        self.progressLabel.hide()
        self._show()
        deferred = any(check["id"] == "hash_deferred" for check in report["checks"])
        if deferred and self._deferredRepeats < MAX_DEFERRED_REPEATS and not report["blocked"]:
            self._deferredRepeats += 1
            self.timer.start()  # the files that were not hashed yet are hashed by the next preflight
        elif not deferred:
            self._deferredRepeats = 0

    def currentReport(self):
        """A report of the current settings: the last one when it is fresh, else a new one made now (headers only)."""
        signature = self.signature()
        if self.report is not None and self.reportSignature == signature and time.monotonic() - self.reportAt < FRESH_S:
            return self.report
        from LesionNetworkMappingLib import preflight

        kwargs = self._kwargs()
        report = preflight.run_preflight(self.ctx.study.path, self.ctx.bundle.path, **kwargs)
        self._takeReport(report, signature)
        return report

    def _clear(self):
        """Nothing to check (another seed mode, no study or connectome): the card says why and Compute is not gated."""
        if self._mode() != "cohort":
            text = _("The preflight checks run for the Cohort seed.")
        elif self.ctx.study is None:
            text = _("Open a study to check what the run will analyse.")
        else:
            text = _("Choose a connectome to check the run.")
        self.ctx.setComputeEnabled(True, key="preflight")
        if self.report is None and self._state is None and self.summaryLabel.text == text:
            return  # nothing was shown since the last time
        self.report = None
        self.reportSignature = None
        self._state = None
        self._renderRows([])
        widgets.setChipLevel(self.chip, "neutral", _("Not checked"))
        self.summaryLabel.setText(text)
        self.notesButton.hide()

    #
    # Display
    #

    def _counts(self, report):
        counts = report.get("counts") or {}
        return int(counts.get("block", 0)), int(counts.get("warn", 0)), int(counts.get("info", 0))

    def checkedCount(self, report=None):
        """N of "All N checks passed": the checks that apply (the design check only with a design)."""
        return len(CHECKED) + (1 if self._designPath() else 0)

    def _show(self):
        report = self.report
        if report is None:
            self._clear()
            return
        from LesionNetworkMappingLib import preflight

        blocking = preflight.blocking_checks(report)
        nWarn = self._counts(report)[1]
        shown = [check for check in report["checks"] if check["level"] in ("block", "warn")]
        included, subjects = report["counts"].get("included", 0), report["counts"].get("subjects", 0)
        state = ("block", tuple(check["id"] for check in blocking)) if blocking else ("warn", tuple(check["id"] for check in shown)) if nWarn else ("ok", ())
        changed = state != self._state
        self._state = state
        if blocking:
            level, chipText = "danger", _("Blocked")
            text = _("{n} blocking · {included} of {subjects} subjects would be analysed").format(n=len(blocking), included=included, subjects=subjects)
        elif nWarn:
            level, chipText = "warn", _("{n} to review").format(n=nWarn)
            text = _("{included} of {subjects} subjects will be analysed").format(included=included, subjects=subjects)
        else:
            level, chipText = "ok", _("All {n} checks passed").format(n=self.checkedCount())
            text = _("{included} of {subjects} subjects will be analysed").format(included=included, subjects=subjects)
        if self.runFailure is None:  # the chip keeps saying "Run failed" until the next run
            widgets.setChipLevel(self.chip, level, chipText)
        if changed and not self.runFailure:
            self.widget.collapsed = state[0] == "ok"  # opens when something needs a decision, closes when it is all green; the user's own toggling is kept between checks
        self.summaryLabel.setText(text)
        self.chip.setToolTip("\n".join(["• " + item for item in CHECKED + ((DESIGN_CHECK,) if self._designPath() else ())]))
        notes = [check for check in report["checks"] if check["level"] == "info"]
        self._renderRows(shown + (notes if self.showNotes else []))
        self.notesButton.setVisible(bool(notes))
        if notes:
            self.notesButton.setText(_("Hide {n} notes").format(n=len(notes)) if self.showNotes else _("Show {n} notes").format(n=len(notes)))
        if blocking:
            self.ctx.setComputeEnabled(False, _("Preflight: {message}").format(message=blocking[0]["message"]), key="preflight")
        else:
            self.ctx.setComputeEnabled(True, key="preflight")

    def toggleNotes(self):
        self.showNotes = not self.showNotes
        self._show()

    def _renderRows(self, checks):
        for row in self.rows:
            row.hide()
            row.deleteLater()
        self.rows = []
        for check in checks:
            actions = []
            fix = check.get("fix")
            if fix:
                actions.append(self._action(fix, check))
            row = widgets.banner(check["message"], [action for action in actions if action], level=_bannerLevel(check))
            row.setProperty("preflightCheck", check["id"])
            self.rowsLayout.addWidget(row)
            self.rows.append(row)

    def _action(self, fix, check):
        label = fix.get("label") or fix.get("id")
        if fix["id"] == "include-anyway":
            if check["id"] not in REVIEW_OVERRIDES:
                # Native and un-reviewed normalizations are not part of the cohort's lesion list: fix them where they are made
                return (_("Show in Library"), lambda check=check: self.openLibrary(check["subjects"], check["id"]))
            if check["id"] in self.overrides:
                return (_("Exclude again"), lambda check=check: self.setOverride(check["id"], False))
        return (label, lambda fix=fix, check=check: self.applyFix(fix, check))

    #
    # Compute hooks
    #

    def checkBeforeCompute(self):
        """``ctx.beforeCompute``: (ok, message, fix) from a current report; a run that is started also saves the report."""
        if not self.active():
            return (True, "", None)
        try:
            self._syncCohortOverrides()
            report = self.currentReport()
        except Exception as exc:
            logging.exception("Lesion network mapping: the preflight check failed")
            return (False, _("The preflight check failed: {reason}").format(reason=exc), None)
        from LesionNetworkMappingLib import preflight

        blocking = preflight.blocking_checks(report)
        if not blocking:
            self._saveReport(report)  # only a run that is started has a record
            return (True, "", None)
        first = blocking[0]
        fix = first.get("fix")
        action = None
        if fix:
            label, callback = self._action(fix, first)
            action = (label, callback)
        message = first["message"] + (_(" (+{n} more in the Preflight card)").format(n=len(blocking) - 1) if len(blocking) > 1 else "")
        return (False, message, action)

    def _saveReport(self, report):
        """Write the report of a run that is started to the study's derivatives/qc: one file for each started run."""
        from LesionNetworkMappingLib import preflight

        try:
            self.savedPath = preflight.save_report(self.ctx.study, dict(report))  # the copy gets this run's own run_id
        except Exception:
            logging.exception("Lesion network mapping: the preflight report could not be saved")
            self.savedPath = None
        return self.savedPath

    def assertRun(self, job):
        """``ctx.afterRun``: the output assertions of a finished job; the failures (strings) fail the job."""
        kind = getattr(getattr(job, "spec", None), "kind", None)
        if kind not in ASSERTED_KINDS:
            return []
        try:
            failures = self._assertions(job, kind)
        except Exception as exc:
            logging.exception("Lesion network mapping: the output assertions failed to run")
            failures = [{"message": f"the outputs could not be checked: {type(exc).__name__}: {exc}"}]
        messages = [failure["message"] for failure in failures]
        if messages:
            self._failedJobs[job.id] = messages[0]
        return messages

    @staticmethod
    def _argument(args, name):
        for index, value in enumerate(args):
            if value == name and index + 1 < len(args):
                return args[index + 1]
            if value.startswith(name + "="):
                return value.split("=", 1)[1]
        return None

    def _assertions(self, job, kind):
        from LesionNetworkMappingLib import preflight

        args = list(job.args)
        bundlePath = self._argument(args, "--bundle")
        subjects = self._argument(args, "--subjects")
        expectedN = int(subjects) if subjects and subjects.isdigit() else None
        if expectedN is None and bundlePath and self._argument(args, "--subject-list") is None and kind in ("engine", "cohortmaps"):
            bundle = self.ctx.bundle
            if bundle is None or os.path.normpath(bundle.path) != os.path.normpath(bundlePath):
                from LesionNetworkMappingLib.connectome import ConnectomeBundle

                bundle = ConnectomeBundle.open(bundlePath)
            expectedN = bundle.n_subjects
        out = self._argument(args, "--out")
        record = self._argument(args, "--run-json") if kind == "engine" else None
        if record is None and out:
            record = os.path.join(out, ASSERTED_KINDS[kind])
        if record is None:
            return []
        expectedLesions = None
        tsv = self._argument(args, "--lesions-tsv")
        if kind == "cohortmaps" and tsv and os.path.isfile(tsv):
            with open(tsv, encoding="utf-8") as f:
                expectedLesions = max(0, sum(1 for line in f if line.strip()) - 1)
        reference = bundlePath or self._argument(args, "--mask")
        kindName = "map" if kind == "engine" else kind
        if kind == "cohortmaps":
            return self._cohortMapsAssertions(record, expectedN, reference, expectedLesions)
        return preflight.assert_run_outputs(record, expectedN, kind=kindName, reference_header=reference, expected_lesions=expectedLesions)

    @staticmethod
    def _cohortMapsAssertions(mapsTsv, expectedN, reference, expectedLesions):
        """Assertions of a cohort maps run. This runs on the main thread: with many lesions every map's header is checked
        (cheap) and the values of an evenly spaced sample of :data:`MAX_VALUE_CHECKS` maps, not of every map.
        """
        from LesionNetworkMappingLib import cohortmaps, preflight, schema

        try:
            rows = cohortmaps.read_maps_tsv(mapsTsv)
            maps = [row[key] for row in rows for key in cohortmaps.MAP_KEYS if not schema.is_missing(row.get(key))]
        except (OSError, ValueError):
            return preflight.assert_run_outputs(mapsTsv, expectedN, kind="cohortmaps", reference_header=reference, expected_lesions=expectedLesions)
        if len(maps) <= MAX_VALUE_CHECKS:
            return preflight.assert_run_outputs(mapsTsv, expectedN, kind="cohortmaps", reference_header=reference, expected_lesions=expectedLesions)
        failures = preflight.assert_run_outputs(mapsTsv, expectedN, kind="cohortmaps", reference_header=reference, expected_lesions=expectedLesions, check_values=False)
        step = len(maps) / MAX_VALUE_CHECKS
        sample = [maps[int(index * step)] for index in range(MAX_VALUE_CHECKS)]
        failures += [failure for failure in preflight.assert_run_outputs({"maps": sample, "n": expectedN}, expectedN, kind="cohortmaps", reference_header=reference)
                     if failure["code"] in ("non_finite", "constant_map", "unreadable")]
        return failures

    def _onJobFinished(self, job):
        """A job failed its output assertions: say so in the card and make the status chip red (the runner showed it amber)."""
        reason = self._failedJobs.pop(getattr(job, "id", None), None)
        if reason is None and job.state == jobrunner.DONE and getattr(getattr(job, "spec", None), "kind", None) in ASSERTED_KINDS:
            self.clearRunFailure()  # a run whose outputs passed replaces the failure of the previous one
        if reason is None or job.state != jobrunner.FAILED:
            return
        text = _("Run failed: {reason}").format(reason=reason)
        shell = self.ctx.shell()
        if shell is not None:
            short = text if len(text) <= jobrunner.CHIP_REASON_CHARS + 12 else text[: jobrunner.CHIP_REASON_CHARS + 11] + "…"
            try:
                shell.setStatusChip(short, "danger")
            except Exception:
                logging.exception("Lesion network mapping: the status chip could not be set")
        log = getattr(job, "logPath", None)
        self.showRunFailure(text + (f"\n{_('Log')}: {log}" if log else ""))

    def showRunFailure(self, text):
        self.clearRunFailure()
        self.runFailure = widgets.banner(text, (), level="danger")
        self.runFailure.setObjectName("LNMPreflightRunFailure")
        self.failureHost.addWidget(self.runFailure)
        widgets.setChipLevel(self.chip, "danger", _("Run failed"))
        self.widget.collapsed = False

    def clearRunFailure(self):
        if self.runFailure is not None:
            self.runFailure.hide()
            self.runFailure.deleteLater()
            self.runFailure = None
            if self.report is not None:
                self._state = None
                self._show()  # the chip shows the checks again instead of "Run failed"

    def onRunEvent(self, event):
        if event.get("event") in ("start", "queued"):
            self.clearRunFailure()

    def onCohortJobStarted(self, job):
        self.clearRunFailure()

    #
    # Fixes
    #

    def applyFix(self, fix, check):
        """Do what a check's fix says; return whether something was done."""
        handler = {
            "open-library": lambda: self.openLibrary(check["subjects"], check["id"]),
            "rule-fwe": self.switchRuleToFwe,
            "rule-effect-size": self.switchRuleToEffectSize,
            "re-measure": lambda: self.remeasure(check["subjects"]),
            "transform": lambda: self.offerTransform(fix, check),
            "accept-terms": self.acceptTerms,
            "include-anyway": lambda: self.setOverride(check["id"], True),
            "probabilistic": self.setProbabilistic,
            "free-disk": self.explainDisk,
        }.get(fix["id"])
        if handler is None:
            logging.warning(f"Lesion network mapping: the preflight fix {fix['id']!r} is not known")
            return False
        try:
            handler()
        except Exception:
            logging.exception(f"Lesion network mapping: the preflight fix {fix['id']!r} failed")
            return False
        return True

    def openLibrary(self, subjects, checkId=None):
        """Open the Library with the subjects of a check selected (the list shows all subjects, scrolled to the first)."""
        subjects = list(subjects or [])
        shell = self.ctx.shell()
        if shell is not None:
            shell.switchTo("library")
        elif hasattr(slicer.modules, "studio"):
            slicer.util.selectModule("Studio")
        try:
            from StudioLib import library
        except Exception:
            return False
        try:
            for view in list(getattr(library, "_studyViews", [])):
                if hasattr(view, "search"):
                    view.search.setText("")
                if hasattr(view, "setFilter"):
                    view.setFilter("all")
            library._setStudySelection(subjects)
            for view in list(getattr(library, "_studyViews", [])):
                row = view.model.rowOf(subjects[0]) if subjects and hasattr(view, "model") else -1
                if row >= 0:
                    view.table.scrollTo(view.model.index(row, 0))
        except Exception:
            logging.exception("Lesion network mapping: the Library could not be filtered to the subjects")
        return True

    def switchRuleToFwe(self):
        widget = _widget()
        if widget is not None and hasattr(widget, "onSwitchToFwe"):
            widget.onSwitchToFwe()
        else:
            self.ctx.setRule({"kind": "fwe_bonferroni", "alpha": 0.05})

    def switchRuleToEffectSize(self):
        from LesionNetworkMappingLib import thresholds

        rule = thresholds.normalize_rule(RULE_EFFECT_SIZE)
        widget = _widget()
        if widget is not None and hasattr(widget, "setRule"):
            widget.setRule(rule)
        else:
            self.ctx.setRule(rule)

    def acceptTerms(self):
        self.ctx.focusCard("connectome")

    def setProbabilistic(self):
        self.probabilistic = True
        self.recheck(force=True)

    def explainDisk(self):
        message = _("The run needs more disk space than is free in the output folder. Free some space, or choose another study folder.")
        shell = self.ctx.shell()
        if shell is not None:
            shell.toast(message, 6000)
        else:
            slicer.util.infoDisplay(message, windowTitle=_("Preflight"))

    def setOverride(self, checkId, on):
        """Include (or exclude again) the lesions of a review exclusion; recorded in the report and ``ctx.preflightOverrides``."""
        if on:
            self.overrides.add(checkId)
        else:
            self.overrides.discard(checkId)
        self.ctx.preflightOverrides = sorted(self.overrides)
        self._syncCohortOverrides()
        self.recheck(force=True)

    def _syncCohortOverrides(self):
        """Give the Cohort seed the lesions of the review overrides (or take them out again): what is mapped is what was checked."""
        widget = _widget()
        study, bundle = self.ctx.study, self.ctx.bundle
        if widget is None or study is None or not hasattr(widget, "cohortInfo") or not hasattr(widget, "logic") or self._mode() != "cohort":
            return
        wanted = bool(self.overrides & set(REVIEW_OVERRIDES))
        info = widget.cohortInfo
        if info is None or bool(info.get("includeUnreviewed")) == wanted:
            return
        fresh = widget.logic.cohortSeeds(study, bundle, includeUnreviewed=wanted)
        fresh["includeUnreviewed"] = wanted
        widget.cohortInfo = fresh
        try:
            widget._findCohortMaps()
            widget._showCohort()  # the Seed card shows the same lesions as the Compute button
            widget.updateComputeState()
        except Exception:
            logging.exception("Lesion network mapping: the cohort lesion list could not be refreshed")

    def remeasure(self, subjects):
        """Measure the lesion files again (a worker thread; the values go through the study inbox), then check again."""
        if self._measure is not None or not subjects:
            return
        from LesionNetworkMappingLib import preflight

        studyPath, bundlePath = self.ctx.study.path, getattr(self.ctx.bundle, "path", None)
        subjects = list(subjects)
        state = {"done": False, "count": 0, "error": None}

        def work():
            try:
                state["count"] = len(preflight.remeasure(studyPath, subjects, bundlePath, post=True))
            except Exception as exc:
                state["error"] = f"{type(exc).__name__}: {exc}"
            state["done"] = True

        self._measure = state
        self.progressLabel.setText(_("Measuring {n} lesions again…").format(n=len(subjects)))
        self.progressLabel.show()
        thread = threading.Thread(target=work, name="lnm-remeasure", daemon=True)
        thread.start()
        timer = qt.QTimer()
        timer.setInterval(100)

        def poll():
            if not state["done"]:
                return
            timer.stop()
            self._measure = None
            if state["error"]:
                self.progressLabel.setText(_("The lesions could not be measured: {reason}").format(reason=state["error"]))
            else:
                self._applyInbox()
                self.progressLabel.setText(_("Measured {n} lesions.").format(n=state["count"]))
                self.recheck(force=True)

        timer.connect("timeout()", poll)
        timer.start()
        self._measureTimer = timer

    def _applyInbox(self):
        session = None
        shell = self.ctx.shell()
        try:
            from StudioLib import study_ui

            session = study_ui.session()
        except Exception:
            session = None
        if session is not None and hasattr(session, "applyInbox") and getattr(session, "study", None) is self.ctx.study:
            session.applyInbox()
        elif self.ctx.study is not None:
            self.ctx.study.apply_inbox()
        if shell is not None and hasattr(shell, "refreshLibrary"):
            shell.refreshLibrary()

    def offerTransform(self, fix, check):
        """Offer the template-family transform of the lesions (the Library's warp, ``transforms warp`` jobs)."""
        params = fix.get("params") or {}
        source, target = params.get("source"), params.get("target")
        source = ", ".join(source) if isinstance(source, (list, tuple)) else source
        subjects = list(check.get("subjects") or [])
        text = _("Transform {n} lesions from the {source} template family to {target}? The transform file is chosen once and remembered; the original lesions are kept.").format(
            n=len(subjects), source=source, target=target)
        try:
            from StudioLib import study_ui
        except Exception:
            slicer.util.infoDisplay(text, windowTitle=_("Transform the lesions"))
            return
        session = study_ui.session()
        warpable = [sid for sid in subjects if session is not None and study_ui.canWarpToConnectomeSpace(session, sid)]
        if not warpable:
            slicer.util.infoDisplay(text + "\n\n" + _("Open the Library to transform them: only MNI152NLin2009cAsym lesions of a study with an MNI152NLin6Asym connectome can be warped here."),
                                    windowTitle=_("Transform the lesions"))
            self.openLibrary(subjects, check["id"])
            return
        if not slicer.util.confirmYesNoDisplay(text, windowTitle=_("Transform the lesions")):
            return
        shell = self.ctx.shell()
        for sid in warpable:
            if study_ui.warpToConnectomeSpace(sid, shell=shell) is None:
                break  # cancelled: no transform file was chosen
        self.schedule()


CARD = {
    "id": "preflight",
    "title": "Preflight",
    "order": ORDER_PREFLIGHT,
    "modes": ("cohort",),
    "factory": PreflightCard,
}

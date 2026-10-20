"""``MapContext``: what the Map module shares with its card plug-ins.

The Map widget creates one context and passes it to every plug-in factory (``CARD["factory"](ctx)``).
It carries the parameter node, the logic, the job runner, the open study and connectome bundle, the
current run and result, observer lists and the compute hooks. Observers and hooks are plain Python
lists of callables, called on the main thread; an observer that raises is logged and the others
still run. Works in Classic and without a main window (``shell()`` is then None).
"""

import logging
import os

from . import jobrunner as _jobrunner


def moduleDir():
    """The folder of the ``LesionNetworkMapping`` module (holds ``LesionNetworkMappingLib`` and this package)."""
    return _jobrunner.moduleDir()


class RunInfo:
    """The current run of the Map module: its folder, its run.json dict, outputs, N and statistic."""

    def __init__(self, runDir=None, runJson=None, outputs=None, n=None, statistic=None, job=None, partial=False):
        self.runDir = os.fspath(runDir) if runDir else None
        self.runJson = dict(runJson or {})
        self.outputs = list(outputs or [])
        self.n = n
        self.statistic = statistic
        self.job = job
        self.partial = bool(partial)  # a snapshot kept after Stop ("N=312 (partial)")

    def __repr__(self):
        return f"RunInfo(runDir={self.runDir!r}, n={self.n!r}, statistic={self.statistic!r}, partial={self.partial})"


class MapContext:
    """State and hooks shared by the Map module and its plug-ins.

    Attributes: ``parameterNode``, ``logic``, ``runner`` (the application's ``JobRunner``), ``study``
    (``LesionNetworkMappingLib.study.Study`` or None), ``bundle`` (an open connectome bundle or None),
    ``currentRun`` (``RunInfo`` or None), ``resultVolume`` (a volume node or None), ``rule`` (the
    threshold rule dict or None).

    Observer lists (called with no argument unless stated): ``studyChanged``, ``bundleChanged``,
    ``runChanged``, ``resultChanged``, ``ruleChanged``, ``seedChanged``, ``primaryOverrideChanged(mode)``,
    ``computeEnabledChanged``.

    Hook lists:

    - ``beforeCompute``: ``check() -> (ok, message, fixAction)``; ``fixAction`` is None or a
      ``(label, callback)`` pair offered next to the message (see ``checkBeforeCompute()``);
    - ``afterRun``: ``check(runInfo) -> [failure strings]`` applied by the JobRunner to every job
      submitted with this context; ``runInfo`` is the finished ``jobrunner.Job`` (``outputs``,
      ``doneEvent``, ``results``, ``spec.kind``, ``runRoot``...). Any failure makes the job fail.
    """

    OBSERVER_LISTS = ("studyChanged", "bundleChanged", "runChanged", "resultChanged", "ruleChanged", "seedChanged", "primaryOverrideChanged", "computeEnabledChanged")

    def __init__(self, parameterNode=None, logic=None, runner=None):
        self.parameterNode = parameterNode
        self.logic = logic
        self.runner = runner if runner is not None else _jobrunner.instance()
        self.study = None
        self.bundle = None
        self.currentRun = None
        self.resultVolume = None
        self.rule = None
        for name in self.OBSERVER_LISTS:
            setattr(self, name, [])
        self.beforeCompute = []
        self.afterRun = []
        self._seed = (None, None)  # used when there is no parameter node
        self._cards = {}
        self._focusHandler = None
        self._primaryOverrides = {}
        self._computeDisabled = {}

    #
    # Notification
    #

    def _notify(self, listName, *args):
        for callback in list(getattr(self, listName)):
            try:
                callback(*args)
            except Exception:
                logging.exception(f"Lesion network mapping: a {listName} observer failed")

    def setStudy(self, study):
        """Set the open study (or None) and notify ``studyChanged``."""
        self.study = study
        self._notify("studyChanged")

    def setBundle(self, bundle):
        """Set the open connectome bundle (or None) and notify ``bundleChanged``."""
        self.bundle = bundle
        self._notify("bundleChanged")

    def setCurrentRun(self, runInfo):
        """Set the current run (a ``RunInfo`` or None) and notify ``runChanged``."""
        self.currentRun = runInfo
        self._notify("runChanged")

    def setResultVolume(self, volumeNode):
        """Set the result volume (or None) and notify ``resultChanged``."""
        self.resultVolume = volumeNode
        self._notify("resultChanged")

    def setRule(self, rule):
        """Set the threshold rule (a dict, see thresholds.py, or None) and notify ``ruleChanged``."""
        self.rule = rule
        self._notify("ruleChanged")

    #
    # Seed
    #

    def seed(self):
        """``(segmentationNode, segmentId)`` of the lesion seed; (None, None) when there is none."""
        node = self.parameterNode
        if node is None:
            return self._seed
        if hasattr(node, "seedSegmentation"):  # parameterNodeWrapper
            return node.seedSegmentation, (node.seedSegmentId or None)
        if hasattr(node, "GetNodeReference"):  # plain vtkMRMLScriptedModuleNode
            return node.GetNodeReference("seedSegmentation"), (node.GetParameter("seedSegmentId") or None)
        return self._seed

    def setSeed(self, segmentationNode, segmentId):
        """Make ``segmentId`` of ``segmentationNode`` the lesion seed (in the parameter node) and notify ``seedChanged``."""
        node = self.parameterNode
        if node is not None and hasattr(node, "seedSegmentation"):
            node.seedSegmentation = segmentationNode
            node.seedSegmentId = segmentId or ""
        elif node is not None and hasattr(node, "SetNodeReferenceID"):
            node.SetNodeReferenceID("seedSegmentation", segmentationNode.GetID() if segmentationNode is not None else None)
            node.SetParameter("seedSegmentId", segmentId or "")
        else:
            self._seed = (segmentationNode, segmentId or None)
        self._notify("seedChanged")

    def notifySeedChanged(self):
        """Notify ``seedChanged`` after the seed was changed elsewhere (for example in the Seed card's selectors)."""
        self._notify("seedChanged")

    #
    # Cards
    #

    def registerCard(self, cardId, controller):
        """Record the controller (with ``.widget``) or widget of a card, for ``focusCard()``; the Map widget calls it."""
        self._cards[str(cardId)] = controller

    def unregisterCard(self, cardId):
        self._cards.pop(str(cardId), None)

    def card(self, cardId):
        """The registered controller (or widget) of a card, or None."""
        return self._cards.get(str(cardId))

    def setFocusCardHandler(self, handler):
        """``handler(cardId) -> bool`` replaces the default of ``focusCard()`` (the Map widget may scroll its own way)."""
        self._focusHandler = handler

    def focusCard(self, cardId):
        """Show, expand and scroll to a card (for example ``focusCard("normalize")``); return whether it was found."""
        if self._focusHandler is not None:
            try:
                return bool(self._focusHandler(str(cardId)))
            except Exception:
                logging.exception("Lesion network mapping: the focusCard handler failed")
                return False
        controller = self._cards.get(str(cardId))
        widget = getattr(controller, "widget", controller)
        if widget is None or not hasattr(widget, "setVisible"):
            return False
        if widget.property("collapsed") is not None:  # ctkCollapsibleButton
            widget.setProperty("collapsed", False)
        widget.setVisible(True)
        parent = widget.parentWidget()
        while parent is not None:
            if hasattr(parent, "ensureWidgetVisible"):  # QScrollArea
                parent.ensureWidgetVisible(widget)
                break
            parent = parent.parentWidget()
        widget.setFocus()
        return True

    #
    # Primary action and compute state
    #

    def setPrimaryOverride(self, mode, label, callback, enabledFn=None):
        """Make ``callback`` the primary action (the blue button and Ctrl+Enter) in seed ``mode``.

        ``label`` is the button text ("Run everything · 12 lesions"); ``enabledFn() -> bool`` (optional)
        tells whether it can run. Notifies ``primaryOverrideChanged(mode)``.
        """
        self._primaryOverrides[mode] = (label, callback, enabledFn)
        self._notify("primaryOverrideChanged", mode)

    def clearPrimaryOverride(self, mode):
        if self._primaryOverrides.pop(mode, None) is not None:
            self._notify("primaryOverrideChanged", mode)

    def primaryOverride(self, mode):
        """``(label, callback, enabledFn)`` of the primary action override of ``mode``, or None."""
        return self._primaryOverrides.get(mode)

    def setComputeEnabled(self, enabled, reason="", key="default"):
        """Allow or block Compute. A blocked state keeps its ``reason`` (shown as the disabled reason).

        Several plug-ins may block Compute at the same time under different ``key``s; Compute is enabled
        when none blocks it. Notifies ``computeEnabledChanged``.
        """
        changed = False
        if enabled:
            changed = self._computeDisabled.pop(key, None) is not None
        elif self._computeDisabled.get(key) != (reason or ""):
            self._computeDisabled[key] = reason or ""
            changed = True
        if changed:
            self._notify("computeEnabledChanged")

    def computeEnabled(self):
        return not self._computeDisabled

    def computeDisabledReasons(self):
        """Reasons Compute is blocked, in the order they were set."""
        return [reason for reason in self._computeDisabled.values() if reason]

    def checkBeforeCompute(self):
        """Run every ``beforeCompute`` hook; return the list of ``(ok, message, fixAction)`` results.

        A hook that raises gives ``(False, "Check failed: …", None)``. Compute may go on when every ``ok`` is true.
        """
        results = []
        for hook in list(self.beforeCompute):
            try:
                result = hook()
            except Exception as exc:
                logging.exception("Lesion network mapping: a beforeCompute check failed")
                results.append((False, f"Check failed: {exc}", None))
                continue
            if result is None:
                continue
            ok, message, fixAction = (tuple(result) + (None, None))[:3]
            results.append((bool(ok), message or "", fixAction))
        return results

    #
    # Jobs
    #

    def submit(self, spec):
        """Submit a ``jobrunner.JobSpec`` with this context (its afterRun hooks, run root and shell); return the ``Job``."""
        return self.runner.submit(spec, context=self)

    #
    # Places
    #

    def moduleDir(self):
        return moduleDir()

    def runRoot(self):
        """Output root of runs: the study's ``derivatives/lnm``, else ``<Slicer cache>/LNM/runs``."""
        study = self.study
        path = getattr(study, "path", None) if study is not None else None
        if path:
            return os.path.join(os.fspath(path), "derivatives", "lnm")
        return _jobrunner.defaultRunRoot()

    def shell(self):
        """The Studio shell (``StudioLib.shell.instance()``), or None in Classic, under tests or without a main window."""
        return _jobrunner._studioShell()

    def cleanup(self):
        """Forget observers, hooks and cards (the Map widget calls it when it is destroyed). Running jobs keep running."""
        for name in self.OBSERVER_LISTS:
            getattr(self, name).clear()
        self.beforeCompute.clear()
        self.afterRun.clear()
        self._cards.clear()
        self._primaryOverrides.clear()
        self._computeDisabled.clear()
        self._focusHandler = None

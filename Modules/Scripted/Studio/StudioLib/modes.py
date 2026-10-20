"""Mode pill of Slicer Studio: every mouse mode is visible and has an on-screen exit (USABILITY 11).

Slicer's mouse modes have no visible state: after "Place a point" or "Adjust window/level" in a view's context
menu, a left-drag does something else until the user finds Esc. While such a mode is active in Studio, a pill at
the top centre of the view area says so and offers the exit:

- Place, one point: "Placing <list> point · click in a view · Esc to cancel";
- Place, persistent (the lock of the markups toolbar): "Placing <list> points · Esc to stop";
- Adjust window/level: "Window/level · drag in a view · Esc to exit".

Place deselects the active Segment Editor tool. Draw records it as *suspended* and arms it again when the mode
ends (``draw.suspendedEffect()``), so the pill appends " · Brush resumes after" to tell the user that the brush
comes back. The pill's ✕ calls ``SwitchToViewTransformMode()`` like Esc (``Shell.handleEscape()``, rung 1).

The pill is hidden in the default (view transform) mode, while a canvas covers the views (Library), and in
Classic (the plug-in is only installed while Studio is applied). It is a ``hud.CornerPill`` named
``StudioModePill``, a child of the central widget, above the views and the canvas and below popups.

State: the plug-in observes the application interaction node (InteractionModeChangedEvent, ModifiedEvent for the
persistence flag) and the selection node (the active place node), and coalesces all events in a 0 ms single-shot
timer. While a mode is active, a 250 ms timer checks whether a canvas covers the views (the shell does not
signal that); nothing runs while the default mode is active, and nothing runs on mouse hover.

This module is an optional plug-in of the Studio shell: ``install(shell)`` / ``uninstall(shell)``.
"""

import logging
import traceback

import qt
import slicer
import vtk
from slicer.i18n import tr as _

from . import hud, ui

PILL_OBJECT_NAME = "StudioModePill"
#: Distance of the pill from the top of the view area (the central widget): below the controller bar of the slice
#: views (about 31 px, its slider and readout stay reachable) and below the letter at the top edge of the view
#: (6 px margin, about 16 px high), which the pill would hide in a one-view layout
PILL_TOP = 62
#: Space kept free on each side of the pill, and in the focus mode, where the "Show panels" pill is at the top right
PILL_SIDE_RESERVE = 12
PILL_FOCUS_MODE_RESERVE = 150
#: While a mode is active: how often the pill checks whether a canvas covers the views
CANVAS_CHECK_MS = 250
#: Longest list name shown in the pill
MAX_NAME_LENGTH = 24

_controller = None


def _shortName(name):
    name = str(name or "").strip()
    return name if len(name) <= MAX_NAME_LENGTH else name[:MAX_NAME_LENGTH - 1].rstrip() + "…"


def toolLabel(effectName):
    """The Studio name of a Segment Editor tool ("Paint" -> "Brush"), or the name itself if Draw has none."""
    if not effectName:
        return ""
    try:
        from . import draw
        appearance = getattr(draw, "EFFECT_APPEARANCE", {}).get(effectName)
    except Exception:
        appearance = None
    return str(appearance[0]) if appearance else str(effectName)


def resumingTool(shell=None):
    """The Draw tool that the current mode suspended ("Paint"), None if there is none or Draw does not say."""
    try:
        from . import draw
        function = getattr(draw, "suspendedEffect", None)
        return function(shell) if function is not None else None
    except Exception:
        logging.error(f"Studio: cannot ask Draw for the suspended tool:\n{traceback.format_exc()}")
        return None


def modeText(mode, persistent=False, placeName="", tool=""):
    """Text of the pill for an interaction mode (None in the default mode).

    ``mode`` is a ``vtkMRMLInteractionNode`` mode, ``placeName`` the name of the active point list ("" if there is
    none) and ``tool`` the Studio name of the Draw tool that resumes when the mode ends ("Brush").
    """
    if mode == slicer.vtkMRMLInteractionNode.Place:
        name = _shortName(placeName)
        if persistent:
            text = _("Placing {name} points · Esc to stop").format(name=name) if name else _("Placing points · Esc to stop")
        else:
            text = (_("Placing {name} point · click in a view · Esc to cancel").format(name=name) if name
                    else _("Placing a point · click in a view · Esc to cancel"))
    elif mode == slicer.vtkMRMLInteractionNode.AdjustWindowLevel:
        text = _("Window/level · drag in a view · Esc to exit")
    else:
        return None
    if tool:
        text += _(" · {tool} resumes after").format(tool=tool)
    return text


class ModePill(hud.CornerPill):
    """The pill: a translucent button at the top centre of the view area; clicking it (its ✕) exits the mode."""

    def __init__(self, parentWidget, callback, reserveFunction=None):
        hud.CornerPill.__init__(self, parentWidget, PILL_OBJECT_NAME, "", "x", callback, ui.tooltip(_("Back to navigating"), "Esc"))
        self.fullText = ""
        self.reserveFunction = reserveFunction
        # The ✕ icon after the text
        self.button.setLayoutDirection(qt.Qt.RightToLeft)
        self.button.setSizePolicy(qt.QSizePolicy.Fixed, qt.QSizePolicy.Fixed)

    def _styleSheet(self):
        objectName = self.button.objectName
        background = ui.token("tooltip")
        text = ui.token("tooltip-text")
        # A mode must be seen: more opaque than the focus-mode pill
        return (
            f"QToolButton#{objectName} {{ background-color: {ui._rgba(background, 0.88)}; color: {text.name()};"
            f" border: 1px solid {ui._rgba(text, 0.25)}; border-radius: {hud.PILL_HEIGHT // 2}px; padding: 0px 10px 0px 14px; font-size: 12px; }}"
            f"QToolButton#{objectName}:hover {{ background-color: {background.name()}; }}")

    def setModeText(self, text):
        """Set the text; it is elided when the view area is too narrow (the accessible name keeps it all)."""
        self.fullText = text
        self.button.setAccessibleName(text)
        self.button.setAccessibleDescription(_("Click to leave this mode, or press Esc"))
        self._fit()

    def _availableWidth(self):
        reserve = PILL_SIDE_RESERVE + (self.reserveFunction() if self.reserveFunction is not None else 0)
        return max(160, self.parentWidget.width - 2 * reserve)

    def _fit(self):
        text = self.fullText
        self.button.setText(text)
        self.button.adjustSize()
        available = self._availableWidth()
        if self.button.width > available:
            font = qt.QFont(self.button.font)
            font.setPixelSize(12)
            metrics = qt.QFontMetrics(font)
            reserve = self.button.width - self._textWidth(metrics, text)
            self.button.setText(metrics.elidedText(text, qt.Qt.ElideRight, max(40, available - reserve)))
            self.button.adjustSize()
            self.button.resize(min(self.button.width, available), hud.PILL_HEIGHT)

    @staticmethod
    def _textWidth(metrics, text):
        advance = getattr(metrics, "horizontalAdvance", None)
        return advance(text) if advance is not None else metrics.width(text)

    def updatePosition(self):
        self._fit()
        self.button.move(max(0, (self.parentWidget.width - self.button.width) // 2), PILL_TOP)

    def show(self):
        self.updatePosition()
        self.button.show()
        self.button.raise_()


class _ResizeFilter(qt.QObject):
    """Keeps the pill centred while the view area is resized."""

    def __init__(self, callback):
        qt.QObject.__init__(self)
        self.callback = callback

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize:
            self.callback()
        return False


def _isAlive(qobject):
    try:
        qobject.objectName
    except Exception:
        return False
    return True


class ModeController:
    """Observes the interaction and selection nodes and shows or hides the pill."""

    def __init__(self, shell):
        self.shell = shell
        self.centralWidget = shell.mainWindow.centralWidget()
        self.pill = ModePill(self.centralWidget, self.exitMode, self._focusModeReserve)
        self._observed = []  # [(node, tag)]
        self.interactionNode = None
        self.selectionNode = None
        self.disposed = False
        self.updateTimer = qt.QTimer()
        self.updateTimer.setSingleShot(True)
        self.updateTimer.setInterval(0)
        self.updateTimer.connect("timeout()", self.refresh)
        self.canvasTimer = qt.QTimer()
        self.canvasTimer.setInterval(CANVAS_CHECK_MS)
        self.canvasTimer.connect("timeout()", self._syncVisibility)
        self.resizeFilter = _ResizeFilter(self._onResized)
        self.centralWidget.installEventFilter(self.resizeFilter)
        self.sceneTags = [slicer.mrmlScene.AddObserver(event, self._onSceneChanged)
                          for event in (slicer.vtkMRMLScene.EndCloseEvent, slicer.vtkMRMLScene.EndImportEvent)]
        # Python event filters and observers must not run while the interpreter is finalized
        slicer.app.connect("aboutToQuit()", self.dispose)
        self._bind()
        self.refresh()

    #
    # Observers
    #

    def _bind(self):
        """(Re)observe the interaction and selection nodes of the application logic."""
        logic = slicer.app.applicationLogic()
        interactionNode = logic.GetInteractionNode() if logic is not None else None
        selectionNode = logic.GetSelectionNode() if logic is not None else None
        if interactionNode is self.interactionNode and selectionNode is self.selectionNode and self._observed:
            return
        self._unbind()
        self.interactionNode = interactionNode
        self.selectionNode = selectionNode
        if interactionNode is not None:
            for event in (slicer.vtkMRMLInteractionNode.InteractionModeChangedEvent, vtk.vtkCommand.ModifiedEvent):
                self._observed.append((interactionNode, interactionNode.AddObserver(event, self._onNodeEvent)))
        if selectionNode is not None:
            for event in (slicer.vtkMRMLSelectionNode.ActivePlaceNodeIDChangedEvent,
                          slicer.vtkMRMLSelectionNode.ActivePlaceNodeClassNameChangedEvent):
                self._observed.append((selectionNode, selectionNode.AddObserver(event, self._onNodeEvent)))

    def _unbind(self):
        for node, tag in self._observed:
            try:
                node.RemoveObserver(tag)
            except Exception:
                pass
        self._observed = []
        self.interactionNode = None
        self.selectionNode = None

    def _onNodeEvent(self, caller=None, event=None):
        # Several events follow one change (mode, persistence, active list): refresh once
        if not self.updateTimer.isActive():
            self.updateTimer.start()

    def _onSceneChanged(self, caller=None, event=None):
        self._bind()
        self._onNodeEvent()

    def _focusModeReserve(self):
        focusMode = getattr(self.shell, "focusMode", None)
        return PILL_FOCUS_MODE_RESERVE if focusMode is not None and focusMode() else 0

    def _onResized(self):
        if self.pill.isVisible():
            self.pill.updatePosition()

    #
    # State
    #

    def _placeName(self):
        selectionNode = self.selectionNode
        nodeId = selectionNode.GetActivePlaceNodeID() if selectionNode is not None else ""
        node = slicer.mrmlScene.GetNodeByID(nodeId) if nodeId else None
        return node.GetName() if node is not None else ""

    def text(self):
        """The text of the pill for the current mode (None in the default mode)."""
        interactionNode = self.interactionNode
        if interactionNode is None:
            return None
        mode = interactionNode.GetCurrentInteractionMode()
        if mode not in (slicer.vtkMRMLInteractionNode.Place, slicer.vtkMRMLInteractionNode.AdjustWindowLevel):
            return None
        tool = toolLabel(resumingTool(self.shell))
        return modeText(mode, interactionNode.GetPlaceModePersistence() == 1, self._placeName(), tool)

    def _canvasShown(self):
        canvas = self.shell.canvas() if hasattr(self.shell, "canvas") else None
        if canvas is None:
            return False
        try:
            return canvas.isVisible()
        except Exception:
            return False

    def refresh(self):
        """Show the pill for an active mode, hide it otherwise or over a canvas."""
        if self.disposed:
            return
        try:
            self._bind()
            text = self.text()
            if text is None or not self.shell.isActive():
                self.canvasTimer.stop()
                self.pill.hide()
                return
            self.pill.setModeText(text)
            if not self.canvasTimer.isActive():
                self.canvasTimer.start()
            if self._canvasShown():
                self.pill.hide()
            else:
                self.pill.show()
        except Exception:
            logging.error(f"Studio: mode pill update failed:\n{traceback.format_exc()}")

    def _syncVisibility(self):
        """Hide the pill while a canvas covers the views, show it again when the canvas is closed."""
        try:
            if self.text() is None:
                self.refresh()
                return
            covered = self._canvasShown()
            if covered and self.pill.isVisible():
                self.pill.hide()
            elif not covered and not self.pill.isVisible():
                self.refresh()
        except Exception:
            logging.error(f"Studio: mode pill check failed:\n{traceback.format_exc()}")

    def exitMode(self):
        """The pill's ✕: back to the default mode, like Esc."""
        for node in slicer.util.getNodesByClass("vtkMRMLInteractionNode"):
            node.SwitchToViewTransformMode()
        if self.interactionNode is not None:
            self.interactionNode.SwitchToViewTransformMode()

    def dispose(self):
        if self.disposed:
            return
        self.disposed = True
        self.updateTimer.stop()
        self.canvasTimer.stop()
        try:
            slicer.app.disconnect("aboutToQuit()", self.dispose)
        except Exception:
            pass
        self._unbind()
        for tag in self.sceneTags:
            slicer.mrmlScene.RemoveObserver(tag)
        self.sceneTags = []
        if _isAlive(self.centralWidget):
            self.centralWidget.removeEventFilter(self.resizeFilter)
        for timer, slot in ((self.updateTimer, self.refresh), (self.canvasTimer, self._syncVisibility)):
            try:
                timer.disconnect("timeout()", slot)
            except Exception:
                pass
        self.pill.dispose()


def instance():
    """The controller while the plug-in is installed (None in Classic). Used by the tests."""
    return _controller


def pill():
    """The mode pill widget (a QToolButton named StudioModePill), None in Classic."""
    return _controller.pill.button if _controller is not None else None


def install(shell):
    """Show the mode pill while a Place or window/level mode is active, while the Studio shell is active."""
    global _controller
    uninstall(shell)
    if shell is None or not hasattr(shell, "isActive") or not shell.isActive():
        return
    _controller = ModeController(shell)


def uninstall(shell=None):
    """Remove the pill, the observers and the timers."""
    global _controller
    if _controller is None:
        return
    controller = _controller
    _controller = None
    controller.dispose()

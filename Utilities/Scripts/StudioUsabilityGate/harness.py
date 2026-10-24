"""Driver of the usability gate: the ONLY way a task acts, and the source of its action counts.

A task is a generator: ``yield from gate.click(widget)`` sends one click and returns whether it was sent. The count of
human actions is derived from the input events that the driver sends (USABILITY section 16):

- 1 per click, double-click, key chord, drag, wheel burst to one target, file drop and dialog confirmation;
- a typed form field counts 1; search queries count 1 per character (``perCharacter=True``), Enter is a key;
- hovers, waits and reading count 0.

Before every click or drag the driver checks that the target can be found by a user: it must be visible, enabled and
not covered (the widget under the pointer at the centre of the target is the target or one of its children), and not
clipped by the viewport of its scroll area (for example scrolled under the inspector's sticky footer: it is scrolled into
view first, 1 action). The driver also waits until the target stays still (a control that moves just before the click is
a layout shift, recorded as a warning). A control that fails the check is NOT driven; the step records the failed
assertion ``not discoverable: <reason>``.
Controls inside a hover popup (``ctkPopupWidget``, the popup of a view bar) are driven only after a ``gate.hover(opener)``
and are reported as hover-revealed (warning and the assertion ``hover-only-control``).

Two input backends send the events: ``xdotool`` (real X events under xvfb: mouse, keyboard, wheel; enabled when
DISPLAY is set and xdotool is on the path) and ``synthetic`` (Qt events sent with ``QApplication.sendEvent``).
Qt 5 and Qt 6 safe: key sequences are parsed from strings, ``QFontMetrics.horizontalAdvance`` is used for text
widths, ``event.position()`` is not needed, menus are opened by clicking, never with ``exec()``.
"""

import json
import os
import re
import shutil
import subprocess
import time
import traceback

import qt
import slicer

#: Default settle time after an input event, in ms
SETTLE_MS = 150

_MODIFIER_NAMES = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "meta": "super", "cmd": "super"}
_XDOTOOL_KEYS = {
    "esc": "Escape", "escape": "Escape", "enter": "Return", "return": "Return", "tab": "Tab", "space": "space",
    "backspace": "BackSpace", "del": "Delete", "delete": "Delete", "pageup": "Prior", "pagedown": "Next",
    "left": "Left", "right": "Right", "up": "Up", "down": "Down", "home": "Home", "end": "End", "ins": "Insert",
    "\\": "backslash", "/": "slash", ".": "period", ",": "comma", "+": "plus", "-": "minus", "=": "equal",
    "[": "bracketleft", "]": "bracketright", ";": "semicolon", "'": "apostrophe", "`": "grave", "@": "at",
    "?": "question", "<": "less", ">": "greater",
}
_QT_KEYS = {
    "esc": "Escape", "escape": "Escape", "enter": "Return", "return": "Return", "tab": "Tab", "space": "Space",
    "backspace": "Backspace", "del": "Delete", "delete": "Delete", "pageup": "PageUp", "pagedown": "PageDown",
    "left": "Left", "right": "Right", "up": "Up", "down": "Down", "home": "Home", "end": "End", "ins": "Insert",
    "\\": "Backslash", "/": "Slash", ".": "Period", ",": "Comma", "+": "Plus", "-": "Minus", "=": "Equal",
    "[": "BracketLeft", "]": "BracketRight", ";": "Semicolon",
}


def parseKeySequence(sequence):
    """Split ``"Ctrl+Shift+S"`` into ``(modifiers, key)``: a list of lowercase modifier names and the key name ("+" is a key too)."""
    text = sequence.strip()
    key = "+" if text == "+" or text.endswith("++") else text.split("+")[-1].strip()
    head = text[: len(text) - len(key)].rstrip("+")
    modifiers = []
    for part in [part for part in head.split("+") if part.strip()]:
        name = part.strip().lower()
        if name not in _MODIFIER_NAMES:
            raise ValueError(f"Unknown modifier {part!r} in {sequence!r}")
        modifiers.append(_MODIFIER_NAMES[name])
    return modifiers, key


def toXdotoolChord(sequence):
    modifiers, key = parseKeySequence(sequence)
    name = _XDOTOOL_KEYS.get(key.lower())
    if name is None:
        if re.fullmatch(r"[Ff]\d{1,2}", key):
            name = key.upper()
        elif len(key) == 1:
            name = key.lower() if key.isalpha() else key
        else:
            name = key
    return "+".join(modifiers + [name])


def _qtModifiers(modifiers):
    flags = qt.Qt.NoModifier
    for name in modifiers:
        flags = flags | {"ctrl": qt.Qt.ControlModifier, "alt": qt.Qt.AltModifier, "shift": qt.Qt.ShiftModifier, "super": qt.Qt.MetaModifier}[name]
    return flags


def toQtKey(sequence):
    """``(key code, modifiers, text)`` of a key sequence for a synthetic QKeyEvent."""
    modifiers, key = parseKeySequence(sequence)
    qtName = _QT_KEYS.get(key.lower())
    if qtName is not None:
        code = getattr(qt.Qt, "Key_" + qtName)
    elif re.fullmatch(r"[Ff]\d{1,2}", key):
        code = getattr(qt.Qt, "Key_" + key.upper())
    elif len(key) == 1:
        code = ord(key.upper())
    else:
        raise ValueError(f"Unknown key {key!r}")
    text = ""
    if len(key) == 1 and not any(name in ("ctrl", "alt", "super") for name in modifiers):
        text = key.upper() if "shift" in modifiers else key
    elif key.lower() == "space":
        text = " "
    elif key.lower() in ("enter", "return"):
        text = "\r"
    return code, _qtModifiers(modifiers), text


def sameObject(first, second):
    """True if two PythonQt wrappers refer to the same C++ object."""
    if first is None or second is None:
        return False
    if first is second:
        return True
    try:
        return bool(first == second)
    except Exception:
        return False


def isAncestorOrSelf(widget, hit):
    """True if ``hit`` is ``widget`` or one of its descendants."""
    current = hit
    guard = 0
    while current is not None and guard < 64:
        if sameObject(current, widget):
            return True
        parent = getattr(current, "parentWidget", None)
        current = parent() if callable(parent) else parent
        guard += 1
    return False


def widgetText(widget):
    """A short description of a widget for logs: class, object name and text."""
    if widget is None:
        return "None"
    parts = [widget.className() if hasattr(widget, "className") else str(type(widget))]
    name = getattr(widget, "objectName", "")
    if name:
        parts.append("#" + str(name))
    for attribute in ("text", "title", "windowTitle"):
        try:
            value = getattr(widget, attribute)
            value = value() if callable(value) else value
            if value and isinstance(value, str):
                parts.append(repr(value.replace("&", "")[:40]))
                break
        except Exception:
            pass
    return " ".join(parts)


#
# Input backends
#


class XdotoolBackend:
    """Real X events (xvfb), sent with xdotool."""

    name = "xdotool"

    @staticmethod
    def available():
        return bool(os.environ.get("DISPLAY")) and shutil.which("xdotool") is not None

    def __init__(self, gate):
        self.gate = gate

    def run(self, *arguments):
        result = subprocess.run(["xdotool"] + [str(argument) for argument in arguments], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            self.gate.log("xdotool-error", args=[str(a) for a in arguments], error=result.stderr.strip()[:200])
        return result.stdout.strip()

    def focusWindow(self, window):
        self.run("windowfocus", int(window.winId()))

    def move(self, point):
        self.run("mousemove", point[0], point[1])

    def buttonDown(self, button=1):
        self.run("mousedown", button)

    def buttonUp(self, button=1):
        self.run("mouseup", button)

    def click(self, point, button=1, modifiers=(), double=False):
        for name in modifiers:
            self.run("keydown", name)
        self.run("mousemove", point[0], point[1])
        self.gate.pump(30)
        self.run("click", "--repeat", 2, "--delay", 60, button) if double else self.run("click", button)
        for name in reversed(list(modifiers)):
            self.run("keyup", name)

    def wheel(self, point, notches, modifiers=()):
        self.run("mousemove", point[0], point[1])
        self.gate.pump(30)
        for name in modifiers:
            self.run("keydown", name)
        button = 4 if notches > 0 else 5
        self.run("click", "--repeat", abs(int(notches)), "--delay", 25, button)
        for name in reversed(list(modifiers)):
            self.run("keyup", name)

    def key(self, sequence):
        self.run("key", "--clearmodifiers", toXdotoolChord(sequence))

    def type(self, text):
        self.run("type", "--clearmodifiers", "--delay", 25, text)

    def keyDown(self, name):
        self.run("keydown", name)

    def keyUp(self, name):
        self.run("keyup", name)


class SyntheticBackend:
    """Qt events, sent to the widget under the point (or to the focus widget) with QApplication.sendEvent.

    The events are posted to the event loop (a 0 ms timer, in order) and not sent from the task step: a click may open a modal
    dialog or a menu, whose nested event loop must not run inside the step.
    """

    name = "synthetic"

    @staticmethod
    def available():
        return True

    def __init__(self, gate):
        self.gate = gate
        self._pressed = None
        self._hovered = None

    @staticmethod
    def _post(function):
        qt.QTimer.singleShot(0, function)

    def run(self, *arguments):
        """Same entry point as the xdotool backend (``key``, ``type``...), for the few tasks that send raw X commands: not available."""
        self.gate.log("synthetic-unsupported", args=[str(a) for a in arguments])

    def focusWindow(self, window):
        window.activateWindow()

    @staticmethod
    def _target(point):
        return qt.QApplication.widgetAt(qt.QPoint(int(point[0]), int(point[1])))

    def _mouse(self, eventType, point, button, buttons, modifiers=()):
        widget = self._target(point)
        if widget is None:
            return None
        globalPoint = qt.QPointF(float(point[0]), float(point[1]))
        local = qt.QPointF(widget.mapFromGlobal(qt.QPoint(int(point[0]), int(point[1]))))
        event = qt.QMouseEvent(eventType, local, globalPoint, button, buttons, _qtModifiers(modifiers))
        qt.QApplication.sendEvent(widget, event)
        return widget

    @staticmethod
    def _qtButton(button):
        return {1: qt.Qt.LeftButton, 2: qt.Qt.MiddleButton, 3: qt.Qt.RightButton}[button]

    def _pressedButtons(self):
        return self._qtButton(self._pressed) if self._pressed else qt.Qt.NoButton

    def _enter(self, point):
        """Tell the widget under ``point`` that the pointer entered it (sendEvent of a mouse move does not make Qt do it)."""
        widget = self._target(point)
        if widget is None or sameObject(widget, self._hovered) or not hasattr(qt, "QEnterEvent"):
            return
        self._hovered = widget
        local = qt.QPointF(widget.mapFromGlobal(qt.QPoint(int(point[0]), int(point[1]))))
        qt.QApplication.sendEvent(widget, qt.QEnterEvent(local, local, qt.QPointF(float(point[0]), float(point[1]))))

    def move(self, point):
        def send():
            self._enter(point)
            self._mouse(qt.QEvent.MouseMove, point, qt.Qt.NoButton, self._pressedButtons())

        self._post(send)

    def buttonDown(self, button=1, point=None, modifiers=()):
        point = point or self.gate.lastPoint

        def send():
            self._pressed = button
            self._mouse(qt.QEvent.MouseButtonPress, point, self._qtButton(button), self._qtButton(button), modifiers)

        self._post(send)

    def buttonUp(self, button=1, point=None, modifiers=()):
        point = point or self.gate.lastPoint

        def send():
            self._mouse(qt.QEvent.MouseButtonRelease, point, self._qtButton(button), qt.Qt.NoButton, modifiers)
            self._pressed = None

        self._post(send)

    def click(self, point, button=1, modifiers=(), double=False):
        def send():
            self._enter(point)
            self._mouse(qt.QEvent.MouseMove, point, qt.Qt.NoButton, qt.Qt.NoButton)
            for repetition in range(2 if double else 1):
                self._pressed = button
                self._mouse(qt.QEvent.MouseButtonPress, point, self._qtButton(button), self._qtButton(button), modifiers)
                self._mouse(qt.QEvent.MouseButtonRelease, point, self._qtButton(button), qt.Qt.NoButton, modifiers)
                self._pressed = None
                if double and repetition == 0:
                    self._mouse(qt.QEvent.MouseButtonDblClick, point, self._qtButton(button), self._qtButton(button), modifiers)

        self._post(send)

    def wheel(self, point, notches, modifiers=()):
        def send():
            widget = self._target(point)
            if widget is None:
                return
            local = qt.QPointF(widget.mapFromGlobal(qt.QPoint(int(point[0]), int(point[1]))))
            for _ in range(abs(int(notches))):
                event = qt.QWheelEvent(local, qt.QPointF(float(point[0]), float(point[1])), qt.QPoint(0, 0), qt.QPoint(0, 120 if notches > 0 else -120),
                                       qt.Qt.NoButton, _qtModifiers(modifiers), qt.Qt.NoScrollPhase, False)
                qt.QApplication.sendEvent(widget, event)

        self._post(send)

    @staticmethod
    def _keyTarget():
        # Qt delivers the keys of an open popup to its focus widget (the search field of the Search popup), not to the popup frame
        popup = qt.QApplication.activePopupWidget()
        focus = qt.QApplication.focusWidget()
        if popup is not None:
            return focus if focus is not None and (focus is popup or popup.isAncestorOf(focus)) else popup
        return focus or qt.QApplication.activeModalWidget() or slicer.util.mainWindow()

    def key(self, sequence):
        code, modifiers, text = toQtKey(sequence)

        def send():
            target = self._keyTarget()
            qt.QApplication.sendEvent(target, qt.QKeyEvent(qt.QEvent.KeyPress, code, modifiers, text))
            qt.QApplication.sendEvent(target, qt.QKeyEvent(qt.QEvent.KeyRelease, code, modifiers, text))

        self._post(send)

    def type(self, text):
        def send():
            for character in text:
                target = self._keyTarget()
                code = ord(character.upper()) if character.isalpha() else ord(character)
                qt.QApplication.sendEvent(target, qt.QKeyEvent(qt.QEvent.KeyPress, code, qt.Qt.NoModifier, character))
                qt.QApplication.sendEvent(target, qt.QKeyEvent(qt.QEvent.KeyRelease, code, qt.Qt.NoModifier, character))

        self._post(send)

    def keyDown(self, name):
        pass

    def keyUp(self, name):
        pass


#
# The gate
#


class Step:
    """One driver call of a task (the step list of ``results.json``)."""

    __slots__ = ("actions", "description", "extra", "kind", "target")

    def __init__(self, kind, description, actions, target="", extra=None):
        self.kind = kind
        self.description = description
        self.actions = actions
        self.target = target
        self.extra = extra or {}

    def asDict(self):
        entry = {"kind": self.kind, "desc": self.description, "actions": self.actions}
        if self.target:
            entry["target"] = self.target
        entry.update(self.extra)
        return entry


class Gate:
    """State of a gate run: input backend, current task, steps, assertions, screenshots and log."""

    def __init__(self, out, size=None, inputMode="auto", wave=1, quiet=False, logName="log"):
        self.out = os.path.abspath(out)
        os.makedirs(self.out, exist_ok=True)
        self.wave = wave
        #: Settings of the run (viaRc, fast, data folder...), constant for all tasks
        self.config = {}
        self.metrics = {}
        self.isLastJob = True
        self.quiet = quiet
        self.requestedSize = size
        if inputMode == "xdotool" or (inputMode == "auto" and XdotoolBackend.available()):
            if not XdotoolBackend.available():
                raise RuntimeError("The xdotool input backend needs DISPLAY and xdotool")
            self.backend = XdotoolBackend(self)
        else:
            self.backend = SyntheticBackend(self)
        self.taskName = None
        self.steps = []
        self.actions = 0
        self.checks = []
        self.warnings = []
        self.shots = []
        self.notes = {}
        self.lastPoint = (0, 0)
        self.hoverOpener = None
        self._shotCounter = 0
        os.makedirs(os.path.join(self.out, "log"), exist_ok=True)
        self._logPath = os.path.join(self.out, "log", f"{logName}.jsonl")
        self._modalHandlers = []
        self.fixtures = None

    # logging
    def log(self, kind, **fields):
        fields["kind"] = kind
        fields["task"] = self.taskName
        fields["t"] = round(time.time(), 2)
        with open(self._logPath, "a") as stream:
            stream.write(json.dumps(fields, default=str) + "\n")
        if not self.quiet:
            print("GATE", json.dumps(fields, default=str)[:400], flush=True)

    # task bookkeeping
    def beginTask(self, name):
        self.taskName = name
        self.steps = []
        self.actions = 0
        self.checks = []
        self.warnings = []
        self.shots = []
        self.notes = {}
        self.metrics = {}
        self._shotCounter = 0
        self.hoverOpener = None
        self.log("task-begin")

    def endTask(self):
        self.log("task-end", actions=self.actions)

    def addStep(self, kind, description, actions, target="", **extra):
        self.actions += actions
        step = Step(kind, description, actions, target, extra)
        self.steps.append(step)
        self.log("step", stepKind=kind, desc=description, actions=actions, total=self.actions, target=target, **extra)
        return step

    def note(self, key, value):
        self.notes[key] = value

    def metric(self, name, value):
        """Record a sub-score of the task (an action count, or None when the goal cannot be reached)."""
        self.metrics[name] = value
        self.log("metric", name=name, value=value)

    def check(self, assertionId, ok, detail=""):
        """Record the outcome of a state assertion (tasks.ASSERTIONS says what it guards and when it must pass)."""
        record = {"id": assertionId, "ok": bool(ok), "detail": str(detail)[:500]}
        self.checks.append(record)
        self.log("check", **record)
        return bool(ok)

    def skip(self, assertionId, reason):
        record = {"id": assertionId, "ok": None, "detail": reason}
        self.checks.append(record)
        self.log("check", **record)

    def warn(self, text):
        self.warnings.append(text)
        self.log("warning", text=text)

    # time
    @staticmethod
    def pump(ms=0):
        """Process events for ``ms`` (only from code that does not run inside a task step that may open a modal loop)."""
        end = time.time() + ms / 1000.0
        slicer.app.processEvents()
        while time.time() < end:
            slicer.app.processEvents()
            time.sleep(0.01)

    def wait(self, ms=SETTLE_MS):
        yield ms

    def until(self, condition, timeout=10000, step=50, what=""):
        """Yield until ``condition()`` is true; return True, or False after ``timeout`` ms."""
        start = time.time()
        while True:
            try:
                if condition():
                    return True
            except Exception:
                pass
            if (time.time() - start) * 1000 > timeout:
                if what:
                    self.log("timeout", what=what, timeoutMs=timeout)
                return False
            yield step

    # windows and screens
    @property
    def mainWindow(self):
        return slicer.util.mainWindow()

    def screenOf(self, widget=None):
        widget = widget or self.mainWindow
        screen = widget.screen() if callable(getattr(widget, "screen", None)) else None
        if screen is None:
            screen = getattr(slicer.app, "primaryScreen", None)
        return screen

    def screenGeometry(self):
        screen = self.screenOf()
        if screen is not None:
            geometry = screen.geometry
            geometry = geometry() if callable(geometry) else geometry
            return geometry.width(), geometry.height()
        return 1920, 1080

    def fitWindow(self, size=None):
        """Environment setup, not a user action: the window covers the screen (or ``size``) at the origin."""
        width, height = size or self.requestedSize or self.screenGeometry()
        window = self.mainWindow
        window.setGeometry(0, 0, int(width), int(height))
        self.pump(300)
        self.focus()

    def focus(self):
        """Give the keyboard focus to the main window (xvfb has no window manager: nothing gives it back after a dialog)."""
        if qt.QApplication.activeModalWidget() is not None or qt.QApplication.activePopupWidget() is not None:
            return
        window = self.mainWindow
        if qt.QApplication.activeWindow() is None:
            window.raise_()
            window.activateWindow()
            self.backend.focusWindow(window)
            self.pump(60)

    def shot(self, name):
        """Screenshot of the screen (dialogs and popups included): <out>/shots/<task>/NN-name.png."""
        self.pump(80)
        self._shotCounter += 1
        directory = os.path.join(self.out, "shots", self.taskName or "gate")
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{self._shotCounter:02d}-{name}.png")
        saved = False
        try:
            screen = self.screenOf()
            if screen is not None:
                saved = bool(screen.grabWindow(0).save(path))
        except Exception:
            saved = False
        if not saved:
            try:
                saved = bool(self.mainWindow.grab().save(path))
            except Exception:
                saved = False
        if saved:
            self.shots.append(os.path.relpath(path, self.out))
        return path if saved else None

    # finding widgets
    def topLevelsInOrder(self):
        """Popup, modal dialog, other visible top-level windows, then the main window."""
        result = []
        for widget in (qt.QApplication.activePopupWidget(), qt.QApplication.activeModalWidget()):
            if widget is not None:
                result.append(widget)
        for widget in qt.QApplication.topLevelWidgets():
            if widget.isVisible() and not any(sameObject(widget, other) for other in result):
                result.append(widget)
        return result

    def widget(self, objectName, className="QWidget", visibleOnly=True, root=None):
        """The first (visible) widget of this object name in the popup, the modal dialog or the main window; None if absent."""
        roots = [root] if root is not None else self.topLevelsInOrder() + [self.mainWindow]
        for top in roots:
            if top is None:
                continue
            candidates = list(top.findChildren(className)) if top else []
            if objectName and getattr(top, "objectName", "") == objectName:
                candidates.insert(0, top)
            for candidate in candidates:
                if candidate.objectName == objectName and (not visibleOnly or candidate.isVisible()):
                    return candidate
        return None

    def widgets(self, objectName, className="QWidget", root=None):
        found = []
        for top in ([root] if root is not None else [self.mainWindow]):
            for candidate in top.findChildren(className):
                if candidate.objectName == objectName and candidate.isVisible():
                    found.append(candidate)
        return found

    def buttonWithText(self, text, root=None, className="QAbstractButton"):
        """A visible button whose text (without '&') equals ``text`` in the popup, the modal dialog or ``root``."""
        wanted = text.replace("&", "").strip().lower()
        roots = [root] if root is not None else self.topLevelsInOrder() + [self.mainWindow]
        for top in roots:
            if top is None:
                continue
            for button in top.findChildren(className):
                if button.isVisible() and str(button.text).replace("&", "").strip().lower() == wanted:
                    return button
        return None

    def sliceWidget(self, name):
        return slicer.app.layoutManager().sliceWidget(name)

    def sliceView(self, name):
        return self.sliceWidget(name).sliceView()

    def dock(self):
        return self.mainWindow.findChild("QDockWidget", "PanelDockWidget")

    def dockWidth(self):
        dock = self.dock()
        return dock.width if dock is not None and dock.isVisible() else 0

    def shell(self):
        try:
            from StudioLib import shell as shellModule

            return shellModule.instance()
        except Exception:
            return None

    def currentWorkspace(self):
        shell = self.shell()
        return shell.currentWorkspace() if shell is not None else None

    def segmentEditor(self):
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        return getattr(widget, "editor", None) if widget is not None else None

    def effectName(self):
        editor = self.segmentEditor()
        if editor is None:
            return None
        effect = editor.activeEffect()
        return effect.name if effect is not None else None

    def voxelCount(self, segmentationNode=None, segmentId=None):
        """Voxels of a segment (the Segment Editor's current one by default), None if there is none."""
        import numpy as np

        editor = self.segmentEditor()
        if segmentationNode is None and editor is not None:
            segmentationNode = editor.segmentationNode()
            segmentId = editor.currentSegmentID()
        if segmentationNode is None or not segmentId:
            return None
        try:
            return int(np.count_nonzero(slicer.util.arrayFromSegmentBinaryLabelmap(segmentationNode, segmentId)))
        except Exception:
            return None

    # discoverability
    def clickability(self, widget, localPoint=None):
        """``(globalPoint, reason)``: reason is None when a user can click ``widget`` at ``localPoint`` (default: its centre)."""
        if widget is None:
            return None, "the control does not exist"
        try:
            if not widget.isVisible():
                return None, "hidden"
            if not widget.isEnabled():
                return None, "disabled"
            width, height = widget.width, widget.height
        except Exception as error:
            return None, f"deleted ({error})"
        if width <= 0 or height <= 0:
            return None, "zero size"
        x, y = (width // 2, height // 2) if localPoint is None else (int(localPoint[0]), int(localPoint[1]))
        point = widget.mapToGlobal(qt.QPoint(x, y))
        globalPoint = (point.x(), point.y())
        hit = qt.QApplication.widgetAt(qt.QPoint(globalPoint[0], globalPoint[1]))
        if hit is None:
            return None, "outside the screen"
        if not isAncestorOrSelf(widget, hit):
            return None, f"covered by {widgetText(hit)} at ({x}, {y}) of {width}x{height}"
        clipped = self.clippedBy(widget, x, y)
        if clipped is not None:
            # Below (or above) the visible part of its scroll area, for example under the sticky footer of the inspector
            return None, f"clipped by {clipped} at ({x}, {y}) of {width}x{height}"
        return globalPoint, None

    @staticmethod
    def clippedBy(widget, x, y):
        """The scroll area (as text) whose viewport does not show the point ``(x, y)`` of ``widget``, None when it is shown.

        A control scrolled out of the inspector's viewport is not drawn there: what is drawn at its place is what lies
        outside the viewport, such as the inspector's sticky footer.

        Only scroll areas whose viewport holds ``widget`` clip it: a header, scroll bar or corner widget of a scroll area
        is its child but lies outside its viewport (and ``QWidget::mapTo`` to a widget that is not an ancestor crashes
        Qt 5 release builds).
        """
        area = widget.parentWidget()
        guard = 0
        while area is not None and guard < 64:
            if area.inherits("QAbstractScrollArea"):
                viewport = area.viewport()
                if viewport is None or not isAncestorOrSelf(viewport, widget):
                    area = area.parentWidget()
                    guard += 1
                    continue
                local = widget.mapTo(viewport, qt.QPoint(x, y))
                if not viewport.rect.contains(local):
                    return widgetText(area)
            area = area.parentWidget()
            guard += 1
        return None

    @staticmethod
    def isHoverOnly(widget):
        """True if ``widget`` is inside a ``ctkPopupWidget``: the popup of a view bar that opens when the pointer hovers its pin."""
        current = widget
        guard = 0
        while current is not None and guard < 64:
            try:
                if current.inherits("ctkPopupWidget"):
                    return True
            except Exception:
                pass
            parent = getattr(current, "parentWidget", None)
            current = parent() if callable(parent) else parent
            guard += 1
        return False

    def refuse(self, kind, description, reason, target=""):
        """A control that a user cannot reach: not driven, recorded as the failed assertion 'not discoverable'."""
        self.check("not-discoverable", False, f"{description}: {reason}")
        self.addStep(kind + "-refused", description, 0, target, refused=reason)
        return False

    # primitives
    def hover(self, widget, description="", localPoint=None):
        """Move the pointer over a widget (counts 0), at ``localPoint`` (default: its centre). Needed before driving a
        control that a hover reveals, or to read what the view shows under the pointer.
        """
        point, reason = self.clickability(widget, localPoint)
        if reason is not None:
            return self.refuse("hover", description or widgetText(widget), reason, widgetText(widget))
        self.focus()
        self.backend.move(point)
        self.lastPoint = point
        self.hoverOpener = widget
        self.addStep("hover", description or "hover " + widgetText(widget), 0, widgetText(widget))
        yield SETTLE_MS * 4
        return True

    def scrollIntoView(self, widget, description="", setup=False, localPoint=None):
        """Scroll the scroll area that holds ``widget`` (the inspector on a small screen) until the widget (or the point
        ``localPoint`` of it) is in the middle of its viewport, as a person does with the wheel, when it is outside the
        viewport or covered (by the sticky footer of a full inspector): 1 action when a scroll was needed.
        """
        try:
            if widget is None or not widget.isVisible():
                return False
        except Exception:
            return False
        area = widget.parentWidget()
        guard = 0
        while area is not None and not area.inherits("QScrollArea") and guard < 64:
            area = area.parentWidget()
            guard += 1
        if area is None or not area.inherits("QScrollArea"):
            return False
        viewport = area.viewport()
        y = int(localPoint[1]) if localPoint is not None else widget.height // 2
        point = widget.mapTo(viewport, qt.QPoint(0, y))
        _globalPoint, reason = self.clickability(widget, localPoint)
        outside = point.y() < 8 or point.y() > viewport.height - 8
        if not outside and not (reason or "").startswith(("covered", "clipped")):
            return False
        bar = area.verticalScrollBar()
        before = bar.value
        bar.setValue(bar.value + point.y() - viewport.height // 2)
        if bar.value == before:
            return False
        self.addStep("scroll", f"Scroll the panel to {description or widgetText(widget)}", 0 if setup else 1, widgetText(area))
        yield SETTLE_MS
        return True

    def click(self, widget, description="", button=1, localPoint=None, double=False, modifiers=(), settle=SETTLE_MS, setup=False):
        """Click a widget at ``localPoint`` (default: its centre). Return False (and record why) if a user could not.

        A widget below the visible part of a scroll area (the inspector on a small screen) is scrolled into view first
        (``scrollIntoView``, 1 action).
        """
        description = description or "click " + widgetText(widget)
        yield from self.scrollIntoView(widget, description, setup=setup, localPoint=localPoint)
        yield from self.waitStill(widget, description)
        point, reason = self.clickability(widget, localPoint)
        if reason is not None:
            return self.refuse("click", description, reason, widgetText(widget))
        extra = {}
        if self.isHoverOnly(widget):
            extra["hoverRevealed"] = True
            self.warn(f"hover-only control: {description}")
            self.check("hover-only-control", False, f"{description}: shown only while the pointer is over its opener")
        self.focus()
        self.backend.click(point, button, modifiers, double)
        self.lastPoint = point
        kind = "doubleclick" if double else ("rightclick" if button == 3 else "click")
        if setup:
            extra["setup"] = True
        self.addStep(kind, description, 0 if setup else 1, widgetText(widget), **extra)
        yield settle
        return True

    def waitStill(self, widget, description="", timeoutMs=1000, stepMs=100):
        """Wait (no action) until ``widget`` stays at the same place for ``stepMs``: a control that moves just before the
        click (a card above it grew) would take the click somewhere else. A move is recorded as a warning (USABILITY P5).
        """
        def place():
            try:
                point = widget.mapToGlobal(qt.QPoint(0, 0))
                return (point.x(), point.y(), widget.width, widget.height)
            except Exception:
                return None

        first = previous = place()
        waited = 0
        while previous is not None and waited < timeoutMs:
            yield stepMs
            waited += stepMs
            current = place()
            if current == previous:
                break
            previous = current
        if first is not None and previous is not None and first != previous:
            self.warn(f"layout shift before '{description}': {widgetText(widget)} moved from {first[:2]} to {previous[:2]}")
            yield from self.scrollIntoView(widget, description, setup=True)
        return True

    def key(self, sequence, description="", settle=SETTLE_MS):
        """Press one key chord, for example ``"Ctrl+K"`` or ``"Esc"``."""
        self.focus()
        self.backend.key(sequence)
        self.addStep("key", description or f"press {sequence}", 1, sequence)
        yield settle
        return True

    def type(self, widget, text, description="", perCharacter=False, selectAll=False, enter=False, settle=SETTLE_MS):
        """Type text. A form field counts 1 (a click that gives it the focus is part of that action); with ``perCharacter``
        (search queries) every character counts 1. ``widget=None`` types where the focus is.
        """
        description = description or f"type {text!r}"
        extra = {}
        if widget is not None:
            focus = qt.QApplication.focusWidget()
            if not (focus is not None and isAncestorOrSelf(widget, focus)):
                yield from self.scrollIntoView(widget, description)
                point, reason = self.clickability(widget)
                if reason is not None:
                    return self.refuse("type", description, reason, widgetText(widget))
                self.focus()
                self.backend.click(point)
                self.lastPoint = point
                yield 80
        self.focus()
        if selectAll:
            self.backend.key("Ctrl+A")
            yield 40
        self.backend.type(text)
        if enter:
            # Committing a form field with Return is part of filling the field
            yield 60
            self.backend.key("Return")
        count = len(text) if perCharacter else 1
        if perCharacter:
            extra["perCharacter"] = True
        self.addStep("type", description, count, widgetText(widget) if widget is not None else "focus", text=text, **extra)
        yield max(settle, 30 * len(text))
        return True

    def drag(self, widget, points, description="", button=1, modifiers=(), pointsAreGlobal=False, settle=SETTLE_MS, stepMs=25, setup=False):
        """Press at ``points[0]``, move through the others and release (local widget coordinates). Counts 1."""
        description = description or "drag in " + widgetText(widget)
        first = points[0]
        _globalPoint, reason = self.clickability(widget, first)
        if reason is not None:
            return self.refuse("drag", description, reason, widgetText(widget))
        self.focus()
        origin = widget.mapToGlobal(qt.QPoint(0, 0))
        converted = [(origin.x() + int(p[0]), origin.y() + int(p[1])) for p in points]
        for name in modifiers:
            self.backend.keyDown(name)
        self.backend.move(converted[0])
        yield 30
        self.backend.buttonDown(button, converted[0], modifiers) if isinstance(self.backend, SyntheticBackend) else self.backend.buttonDown(button)
        yield stepMs
        for point in converted[1:]:
            self.backend.move(point)
            yield stepMs
        last = converted[-1]
        self.backend.buttonUp(button, last, modifiers) if isinstance(self.backend, SyntheticBackend) else self.backend.buttonUp(button)
        for name in reversed(list(modifiers)):
            self.backend.keyUp(name)
        self.lastPoint = last
        self.addStep("drag", description, 0 if setup else 1, widgetText(widget), points=len(points), **({"setup": True} if setup else {}))
        yield settle
        return True

    def wheel(self, widget, notches, description="", modifiers=(), localPoint=None, settle=SETTLE_MS, setup=False):
        """One wheel burst of ``notches`` (positive: forward/away) over a widget. Counts 1."""
        description = description or f"wheel {notches} over {widgetText(widget)}"
        point, reason = self.clickability(widget, localPoint)
        if reason is not None:
            return self.refuse("wheel", description, reason, widgetText(widget))
        self.focus()
        self.backend.wheel(point, notches, modifiers)
        self.lastPoint = point
        self.addStep("wheel", description, 0 if setup else 1, widgetText(widget), notches=notches, **({"setup": True} if setup else {}))
        yield settle
        return True

    def closeWindow(self, window, description="Close the window"):
        """The window's close button (needs a window manager; here the close event is sent). Counts 1."""
        if not window.isVisible():
            return self.refuse("close", description, "hidden", widgetText(window))
        self.addStep("close", description, 1, widgetText(window))
        qt.QTimer.singleShot(0, window.close)
        yield 400
        return True

    def drop(self, paths, description="", addData=False, settle=800):
        """Drop files or a folder on the window (the importer's drop handler; counts 1 like a drag from the file manager)."""
        from StudioLib import importer

        paths = [str(path) for path in paths]
        self.addStep("drop", description or "drop " + ", ".join(os.path.basename(p) for p in paths), 1, "main window", paths=paths)
        # Through the event loop: the handler may open a modal dialog
        qt.QTimer.singleShot(0, lambda: importer.handleDrop(paths, addData=addData))
        yield settle
        return True

    def clickMenuItem(self, menu, text, description="", settle=SETTLE_MS):
        """Click the item of an open menu whose text starts with ``text`` (counts 1)."""
        wanted = text.replace("&", "").strip().lower()
        action = None
        for candidate in menu.actions():
            label = str(candidate.text).replace("&", "").split("\t")[0].strip().lower()
            if not candidate.isSeparator() and label.startswith(wanted):
                action = candidate
                break
        description = description or f"menu item {text}"
        if action is None:
            return self.refuse("menu", description, "no such item", widgetText(menu))
        if not action.isVisible():
            return self.refuse("menu", description, "hidden", text)
        if not action.isEnabled():
            return self.refuse("menu", description, "disabled", text)
        geometry = menu.actionGeometry(action)
        local = (geometry.x() + geometry.width() // 2, geometry.y() + geometry.height() // 2)
        point = menu.mapToGlobal(qt.QPoint(local[0], local[1]))
        hit = qt.QApplication.widgetAt(point)
        if hit is None or not isAncestorOrSelf(menu, hit):
            return self.refuse("menu", description, f"covered by {widgetText(hit)}", text)
        self.backend.click((point.x(), point.y()))
        self.lastPoint = (point.x(), point.y())
        self.addStep("menu", description, 1, text)
        yield settle
        return True

    def menu(self, opener, path, description=""):
        """Open the menu of ``opener`` by clicking it, then click each item of ``path`` (submenus open on a click). 1 action per click."""
        description = description or " > ".join(path)
        if not (yield from self.click(opener, f"open the menu: {description}")):
            return False
        for index, text in enumerate(path):
            popup = qt.QApplication.activePopupWidget()
            if popup is None or not popup.inherits("QMenu"):
                yield from self.until(lambda: qt.QApplication.activePopupWidget() is not None, 1500)
                popup = qt.QApplication.activePopupWidget()
            if popup is None or not popup.inherits("QMenu"):
                return self.refuse("menu", description, "no menu is open", text)
            if not (yield from self.clickMenuItem(popup, text, f"{description}: {text}", settle=SETTLE_MS * (2 if index < len(path) - 1 else 1))):
                return False
        return True

    def closePopups(self):
        """Environment cleanup between tasks (not an action): close popups and modal dialogs."""
        for _ in range(6):
            popup = qt.QApplication.activePopupWidget()
            modal = qt.QApplication.activeModalWidget()
            target = popup or modal
            if target is None:
                break
            target.close()
            self.pump(50)


#
# Scheduler: runs the task generators from the event loop
#


class Scheduler:
    """Run generators one after the other from a QTimer, so that modal dialogs and menus (nested event loops) never block a task."""

    def __init__(self, gate, jobs, onDone, taskTimeoutSeconds=420):
        self.gate = gate
        self.jobs = list(jobs)  # (name, factory(gate) -> generator, begin(gate), end(gate, error))
        self.onDone = onDone
        self.taskTimeoutSeconds = taskTimeoutSeconds
        self._generator = None
        self._current = None
        self._started = 0.0
        self._running = False
        self.errors = {}

    def start(self, delayMs=0):
        qt.QTimer.singleShot(delayMs, self._nextJob)

    def _nextJob(self):
        if not self.jobs:
            self.onDone()
            return
        name, factory, begin, end = self.jobs.pop(0)
        self._current = (name, end)
        self.gate.beginTask(name)
        self._started = time.time()
        try:
            if begin is not None:
                begin(self.gate)
            self._generator = factory(self.gate)
        except Exception:
            self._finishCurrent(traceback.format_exc())
            return
        qt.QTimer.singleShot(0, self._tick)

    def _tick(self):
        if self._running:
            # A nested event loop ran this timer while a step was executing: try again later
            qt.QTimer.singleShot(20, self._tick)
            return
        self._running = True
        delay = 0
        try:
            if time.time() - self._started > self.taskTimeoutSeconds:
                raise TimeoutError(f"task {self._current[0]} exceeded {self.taskTimeoutSeconds} s")
            delay = next(self._generator)
        except StopIteration:
            self._running = False
            self._finishCurrent(None)
            return
        except Exception:
            self._running = False
            self._finishCurrent(traceback.format_exc())
            return
        self._running = False
        qt.QTimer.singleShot(int(delay or 0), self._tick)

    def _finishCurrent(self, error):
        name, end = self._current
        if error:
            self.errors[name] = error
            self.gate.log("task-error", error=error[-1500:])
            if not self.gate.quiet:
                print(error, flush=True)
        try:
            if end is not None:
                end(self.gate, error)
        except Exception:
            self.gate.log("task-cleanup-error", error=traceback.format_exc()[-1500:])
        self.gate.endTask()
        self._generator = None
        qt.QTimer.singleShot(50, self._nextJob)


#
# Results: classification of the assertions, targets and the reports
#

TARGETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "targets.json")
#: Tolerance of the wave 1 baseline around the "After QW (est.)" column
ESTIMATE_TOLERANCE = 0.15


def loadTargets():
    with open(TARGETS_PATH) as stream:
        return {key: value for key, value in json.load(stream).items() if not key.startswith("_")}


def targetFor(targets, key, wave):
    """The limit of a metric after ``wave`` (None: no limit). Wave 1 allows the estimate of the post-QW build + 15%."""
    entry = targets.get(key)
    if entry is None:
        return None
    if wave <= 1:
        estimate = entry.get("afterQW")
        return int(estimate * (1 + ESTIMATE_TOLERANCE) + 0.5) if estimate is not None else None
    return entry.get(f"target_w{min(wave, 4)}")


def classify(record, wave, assertions):
    """Status of one assertion record: pass, fail (blocks the gate), xfail (expected failure before ``fromWave``), xpass, skipped."""
    meta = assertions.get(record["id"], {})
    fromWave = meta.get("fromWave", 1)
    if record["ok"] is None:
        return "skipped"
    if record["ok"]:
        return "xpass" if fromWave > wave else "pass"
    return "fail" if fromWave <= wave else "xfail"


def summarizeAssertions(records, wave, assertions):
    """``{id: {"status", "details": [...]}}``: failing records dominate, then skipped, then passes."""
    summary = {}
    for record in records:
        status = classify(record, wave, assertions)
        entry = summary.setdefault(record["id"], {"status": status, "details": []})
        rank = {"fail": 5, "xfail": 4, "xpass": 3, "skipped": 2, "pass": 1}
        if rank[status] > rank[entry["status"]]:
            entry["status"] = status
        if record["detail"]:
            entry["details"].append(record["detail"])
    return summary


def buildTaskResult(gate, spec, error, targets, wave, assertions):
    """The ``results.json`` entry of the task that just ran in ``gate``."""
    metrics = dict(gate.metrics)
    headline = metrics.get(spec.headline) if spec.headline else gate.actions
    records = list(gate.checks)
    if error:
        records.append({"id": "task-completed", "ok": False, "detail": error.strip().splitlines()[-1][:300]})
    summary = summarizeAssertions(records, wave, assertions)
    result = {
        "title": targets.get(spec.name, {}).get("title", spec.title),
        "actions": headline,
        "totalActions": gate.actions,
        "metrics": metrics,
        "official": targets.get(spec.name, {}).get("official"),
        "target": targetFor(targets, spec.name, wave),
        "afterQW": targets.get(spec.name, {}).get("afterQW"),
        "assertions": summary,
        "failedAssertions": [f"{key}: {'; '.join(value['details'])[:200]}" for key, value in summary.items() if value["status"] == "fail"],
        "expectedFailures": [f"{key}: {'; '.join(value['details'])[:200]}" for key, value in summary.items() if value["status"] == "xfail"],
        "warnings": list(gate.warnings),
        "notes": dict(gate.notes),
        "steps": [step.asDict() for step in gate.steps],
        "shots": list(gate.shots),
        "error": error.strip().splitlines()[-1] if error else None,
        "inputBackend": gate.backend.name,
    }
    metricTargets = {}
    for name, value in metrics.items():
        key = f"{spec.name}.{name}"
        if key in targets and name != spec.headline:
            metricTargets[name] = {"value": value, "target": targetFor(targets, key, wave), "official": targets[key].get("official")}
    result["metricTargets"] = metricTargets
    return result


def evaluateResults(results, wave, baselineMode=False):
    """Mark each task and metric as pass or over target; return the list of blocking problems (empty: the gate passes)."""
    problems = []
    for name, result in results.items():
        failures = []
        if result.get("error"):
            failures.append(f"error: {result['error']}")
        actions, target = result.get("actions"), result.get("target")
        if target is not None and (actions is None or actions > target):
            failures.append(f"{actions if actions is not None else 'unreachable'} actions > target {target}")
        for metricName, entry in result.get("metricTargets", {}).items():
            if entry["target"] is not None and (entry["value"] is None or entry["value"] > entry["target"]):
                failures.append(f"{metricName}: {entry['value'] if entry['value'] is not None else 'unreachable'} > target {entry['target']}")
        failures.extend(result.get("failedAssertions", []))
        afterQW = result.get("afterQW")
        if wave <= 1 and afterQW and actions is not None:
            result["withinEstimate"] = abs(actions - afterQW) <= ESTIMATE_TOLERANCE * afterQW
        result["pass"] = not failures
        result["failures"] = failures
        if failures and not baselineMode:
            problems.extend(f"{name}: {failure}" for failure in failures)
    return problems


def stepSignature(result):
    return [f"{step['kind']}: {step['desc']}" for step in result.get("steps", [])]


def diffSteps(current, previous):
    """Added and removed steps (by kind and description) between two runs of a task."""
    previousList, currentList = stepSignature(previous), stepSignature(current)
    added = [s for s in currentList if s not in previousList]
    removed = [s for s in previousList if s not in currentList]
    return added, removed


def renderMarkdown(results, meta, previous=None):
    """``results.md``: one table of the counts and, per task, assertions, warnings and the step diff against ``previous``."""
    lines = [f"# Usability gate, wave {meta['wave']}", ""]
    lines.append(f"Build {meta.get('build', '?')}, {meta.get('date', '')}, window {meta.get('size', '?')}, input {meta.get('input', '?')}"
                 + (", BASELINE (no failure)" if meta.get("baseline") else ""))
    lines += ["", "| task | actions | target | after QW (est.) | official | within est. | pass |", "|---|---|---|---|---|---|---|"]
    for name, result in results.items():
        within = result.get("withinEstimate")
        lines.append(f"| {name} | {result.get('actions')} | {result.get('target')} | {result.get('afterQW')} | {result.get('official')} | "
                     f"{'' if within is None else ('yes' if within else 'NO')} | {'PASS' if result.get('pass') else 'FAIL'} |")
    for name, result in results.items():
        lines += ["", f"## {name}", "", result.get("title") or "", ""]
        if result.get("metrics"):
            lines.append("Metrics: " + ", ".join(f"{key}={value}" for key, value in result["metrics"].items()))
        if result.get("error"):
            lines.append(f"ERROR: {result['error']}")
        if result.get("failures"):
            lines += ["", "Blocking:"] + [f"- {failure}" for failure in result["failures"]]
        if result.get("expectedFailures"):
            lines += ["", "Expected failures (pass from the wave named in tasks.ASSERTIONS):"] + [f"- {failure}" for failure in result["expectedFailures"]]
        passed = [key for key, value in result.get("assertions", {}).items() if value["status"] in ("pass", "xpass")]
        skipped = [f"{key} ({'; '.join(value['details'])[:100]})" for key, value in result.get("assertions", {}).items() if value["status"] == "skipped"]
        if passed:
            lines += ["", "Assertions passed: " + ", ".join(passed)]
        if skipped:
            lines += ["", "Assertions skipped: " + "; ".join(skipped)]
        if result.get("warnings"):
            lines += ["", "Warnings:"] + [f"- {text}" for text in result["warnings"][:10]]
        kinds = {}
        for step in result.get("steps", []):
            kinds[step["kind"]] = kinds.get(step["kind"], 0) + step["actions"]
        lines += ["", "Actions by kind: " + ", ".join(f"{key} {value}" for key, value in sorted(kinds.items()) if value)]
        if previous and name in previous:
            added, removed = diffSteps(result, previous[name])
            if added or removed:
                lines += ["", f"Steps changed since the reference run ({len(added)} added, {len(removed)} removed):"]
                lines += [f"- added: {text}" for text in added[:30]] + [f"- removed: {text}" for text in removed[:30]]
        if result.get("shots"):
            lines += ["", "Screenshots: " + ", ".join(result["shots"][:8]) + (" ..." if len(result["shots"]) > 8 else "")]
    return "\n".join(lines) + "\n"


def baselineDocument(results, meta):
    """``baseline.json``: the reference of later runs ({task: counts, assertion statuses and steps, "_meta": ...})."""
    document = {
        name: {
            "actions": result.get("actions"),
            "metrics": result.get("metrics"),
            "assertions": {key: value["status"] for key, value in result.get("assertions", {}).items()},
            "steps": result.get("steps", []),
        }
        for name, result in results.items()
    }
    document["_meta"] = meta
    return document


def loadReference(path):
    """The tasks of a ``baseline.json`` or ``results.json`` (without the ``_meta`` entry), or None."""
    if not path or not os.path.isfile(path):
        return None
    with open(path) as stream:
        document = json.load(stream)
    return {key: value for key, value in document.items() if key != "_meta"}

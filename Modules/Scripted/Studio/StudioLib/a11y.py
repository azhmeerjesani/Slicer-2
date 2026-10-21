"""Keyboard and low-vision access of Slicer Studio (USABILITY 9, U-03).

A Studio plug-in (``shell.OPTIONAL_PLUGINS``): ``install(shell)`` runs when Studio is applied and ``uninstall(shell)``
in Classic, which then behaves exactly as before.

- **AltGr guard** (CM-19). On Windows AltGr is reported as Ctrl+Alt, so AltGr+2 ('@' on several European layouts)
  would run the Ctrl+Alt+2 workspace shortcut instead of typing. While a text input has the keyboard focus (line edit,
  text edit, spin box, Python console), a Ctrl+Alt key that types a printable character is kept by the input. In a
  view, AltGr+digit still acts as Ctrl+Alt+digit (Qt cannot tell them apart): documented in the shortcut list.
- **Focus frame of the views** (CM-04). A slice view that gets the keyboard focus with Tab, Shift+Tab or a shortcut
  (F6) shows a 2 px accent frame; a mouse press or leaving the view hides it. The frame is driven by the application's
  ``focusChanged`` signal; only the view that shows the frame has an event filter. Hovering, rendering and focus
  follows the mouse run no Python code (an event filter on every view would run for each of their paint events).
- **Narrow windows** (CM-15). Below ``shell.NARROW_WINDOW_WIDTH`` the shell makes the top bar compact
  (``TopBar.setCompact``); this plug-in lets the inspector become as narrow as ``shell.MIN_PANEL_WIDTH`` there, and
  gives back its minimum width at ``NARROW_EXIT_WIDTH`` and more (hysteresis).

Nothing is written to the settings.
"""

import logging
import traceback

import qt
import slicer

from . import ui

#: Classes of the widgets that keep printable Ctrl+Alt (AltGr) characters
TEXT_INPUT_CLASSES = ("QLineEdit", "QTextEdit", "QPlainTextEdit", "QAbstractSpinBox", "ctkConsole")
#: Width of the view focus frame
VIEW_FOCUS_FRAME_WIDTH = 2
#: Window width (logical px) at which the inspector gets its usual minimum width back (the narrow mode starts below
#: ``shell.NARROW_WINDOW_WIDTH``; the gap avoids switching back and forth while the window is resized)
NARROW_EXIT_WIDTH = 1160

_instance = None


def instance():
    """The installed plug-in (``Accessibility``), or None."""
    return _instance


def _int(value):
    try:
        return int(value)
    except TypeError:
        # A QFlags value that does not convert (Qt 6 PythonQt)
        return int(getattr(value, "value", 0))


_MODIFIER_MASK = None
_CTRL_ALT = None


def _modifierMasks():
    global _MODIFIER_MASK, _CTRL_ALT
    if _MODIFIER_MASK is None:
        _MODIFIER_MASK = _int(qt.Qt.ShiftModifier) | _int(qt.Qt.ControlModifier) | _int(qt.Qt.AltModifier) | _int(qt.Qt.MetaModifier)
        _CTRL_ALT = _int(qt.Qt.ControlModifier) | _int(qt.Qt.AltModifier)
    return _MODIFIER_MASK, _CTRL_ALT


def isAltGrCharacter(event):
    """True if the key event is Ctrl+Alt (exactly, as Windows reports AltGr) and types a printable character."""
    mask, ctrlAlt = _modifierMasks()
    if (_int(event.modifiers()) & mask) != ctrlAlt:
        return False
    text = event.text()
    return bool(text) and text.isprintable() and not text.isspace()


def isTextInput(widget):
    """True if ``widget`` is a text input that keeps AltGr characters."""
    if widget is None:
        return False
    try:
        return any(widget.inherits(className) for className in TEXT_INPUT_CLASSES)
    except (AttributeError, ValueError):
        return False


class _AltGrGuard(qt.QObject):
    """Event filter of the focused text input: a Ctrl+Alt key that types a character is not a shortcut."""

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.ShortcutOverride and isAltGrCharacter(event):
            # Accepted: no shortcut takes the key, and the widget receives the key press (it types the character)
            event.accept()
            return True
        return False


class _ViewFocusFrame(qt.QObject):
    """The 2 px accent frame of one slice view that got the keyboard focus (USABILITY 9.2).

    The frame is shown by ``Accessibility._onFocusChanged()``. While it is shown, an event filter on the view hides it on
    a mouse press and follows the size of the view; otherwise the view has no filter.
    """

    def __init__(self, owner, name, sliceView, focusWidget):
        qt.QObject.__init__(self)
        self.owner = owner
        self.name = name
        self.sliceView = sliceView
        #: The widget that gets the focus (the VTK render widget of the view)
        self.focusWidget = focusWidget
        self.filtering = False
        self.frame = qt.QFrame(sliceView)
        self.frame.setObjectName("StudioViewFocusFrame")
        self.frame.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        self.frame.setFocusPolicy(qt.Qt.NoFocus)
        self.frame.hide()
        ui.bindStyleSheet(self.frame, lambda: (
            f"QFrame#StudioViewFocusFrame {{ background: transparent; border: {VIEW_FOCUS_FRAME_WIDTH}px solid {ui.token('accent-fg').name()}; }}"))

    def setFiltering(self, enabled):
        if enabled == self.filtering:
            return
        self.filtering = enabled
        if enabled:
            self.focusWidget.installEventFilter(self)
        else:
            self.focusWidget.removeEventFilter(self)

    def isShown(self):
        return self.frame.isVisible()

    def show(self):
        self.updateGeometry()
        self.frame.show()
        self.frame.raise_()
        # Watch for a mouse press and a resize while the frame is shown
        self.setFiltering(True)

    def hide(self):
        self.frame.hide()
        self.setFiltering(False)

    def updateGeometry(self):
        widget = self.focusWidget
        topLeft = self.sliceView.mapFromGlobal(widget.mapToGlobal(qt.QPoint(0, 0)))
        self.frame.setGeometry(topLeft.x(), topLeft.y(), widget.width, widget.height)

    def eventFilter(self, obj, event):
        eventType = event.type()
        if eventType in (qt.QEvent.MouseButtonPress, qt.QEvent.FocusOut):
            self.hide()
        elif eventType == qt.QEvent.Resize:
            self.updateGeometry()
        return False

    def dispose(self):
        self.setFiltering(False)
        ui.bindStyleSheet(self.frame, None)
        self.frame.hide()
        # Not found by name until it is deleted
        self.frame.setObjectName("")
        self.frame.deleteLater()


def _mouseButtonsPressed():
    buttons = qt.QApplication.mouseButtons()
    try:
        return _int(buttons) != 0
    except (TypeError, ValueError):
        return buttons != qt.Qt.NoButton


def isKeyboardFocusChange(widget):
    """True if ``widget`` has just got the focus from the keyboard (Tab, Shift+Tab, F6, a shortcut), not from the mouse.

    Called from ``focusChanged``, after the focus event, whose reason is not available: the focus came from the mouse
    when a button is pressed or when the pointer is over the widget (a click, or focus follows the mouse). Hence Tab
    back into the view under a resting pointer shows no frame.
    """
    if _mouseButtonsPressed():
        return False
    position = widget.mapFromGlobal(qt.QCursor.pos())
    return not widget.rect.contains(position)


class Accessibility:
    """The accessibility plug-in of a Studio shell (AltGr guard, view focus frames, narrow windows)."""

    def __init__(self, shell):
        self.shell = shell
        self.altGrGuard = _AltGrGuard()
        #: The text input that has the AltGr guard
        self._guarded = None
        #: Slice view name -> _ViewFocusFrame
        self.viewFrames = {}
        self._layoutManager = None
        self._narrow = False
        #: Inspector minimum width before the narrow mode lowered it (None when it did not)
        self._panelMinimumWidth = None

    #
    # Install
    #

    def install(self):
        slicer.app.connect("focusChanged(QWidget*,QWidget*)", self._onFocusChanged)
        self._guard(slicer.app.focusWidget())
        self._layoutManager = slicer.app.layoutManager()
        if self._layoutManager is not None:
            self._layoutManager.connect("layoutChanged(int)", self._onLayoutChanged)
        self.updateViewFrames()
        # The width of the top bar follows the window: its resize events give the narrow mode (a filter on the main
        # window would also get the mouse moves that the views pass on)
        topBar = getattr(self.shell, "topBar", None)
        self._resizeTarget = topBar.toolBar if topBar is not None else None
        if self._resizeTarget is not None:
            self._resizeTarget.installEventFilter(self._resizeFilter())
        self.updateNarrowMode()

    def uninstall(self):
        slicer.app.disconnect("focusChanged(QWidget*,QWidget*)", self._onFocusChanged)
        self._guard(None)
        if self._layoutManager is not None:
            self._layoutManager.disconnect("layoutChanged(int)", self._onLayoutChanged)
            self._layoutManager = None
        for viewFrame in self.viewFrames.values():
            viewFrame.dispose()
        self.viewFrames = {}
        if self._resizeWatcher is not None and self._resizeTarget is not None and _isAlive(self._resizeTarget):
            self._resizeTarget.removeEventFilter(self._resizeWatcher)
        self._resizeWatcher = None
        self._resizeTarget = None
        self.setNarrow(False)

    #
    # AltGr guard
    #

    def _guard(self, widget):
        """Move the AltGr guard to ``widget`` if it is a text input (else nowhere)."""
        widget = widget if isTextInput(widget) else None
        if self._guarded is not None and not _isAlive(self._guarded):
            # Deleted with its filter
            self._guarded = None
        if widget == self._guarded:
            return
        if self._guarded is not None:
            self._guarded.removeEventFilter(self.altGrGuard)
        self._guarded = widget
        if widget is not None:
            widget.installEventFilter(self.altGrGuard)

    def _onFocusChanged(self, old, new):
        self._guard(new)
        if not self.viewFrames:
            return
        for viewFrame in self.viewFrames.values():
            if old is not None and viewFrame.focusWidget == old:
                viewFrame.hide()
            elif new is not None and viewFrame.focusWidget == new:
                if isKeyboardFocusChange(new):
                    viewFrame.show()
                else:
                    viewFrame.hide()

    #
    # View focus frames
    #

    def _onLayoutChanged(self, layout=None):
        # The views of the new layout exist when the event loop runs
        qt.QTimer.singleShot(0, self.updateViewFrames)

    def updateViewFrames(self):
        """Give every slice view of the layout a focus frame (views that no longer exist lose theirs)."""
        layoutManager = slicer.app.layoutManager()
        names = list(layoutManager.sliceViewNames()) if layoutManager is not None else []
        for name in list(self.viewFrames):
            viewFrame = self.viewFrames[name]
            if name not in names or not _isAlive(viewFrame.sliceView) or not _isAlive(viewFrame.focusWidget):
                if _isAlive(viewFrame.focusWidget) and _isAlive(viewFrame.frame):
                    viewFrame.dispose()
                del self.viewFrames[name]
        for name in names:
            if name in self.viewFrames:
                continue
            sliceWidget = layoutManager.sliceWidget(name)
            if sliceWidget is None:
                continue
            sliceView = sliceWidget.sliceView()
            focusWidget = sliceView.VTKWidget() if hasattr(sliceView, "VTKWidget") else None
            try:
                self.viewFrames[name] = _ViewFocusFrame(self, name, sliceView, focusWidget or sliceView)
            except Exception:
                logging.error(f"Studio: cannot add the focus frame of the {name} view:\n{traceback.format_exc()}")

    def viewFrame(self, name):
        """The focus frame widget of the slice view ``name``, or None."""
        viewFrame = self.viewFrames.get(name)
        return viewFrame.frame if viewFrame is not None else None

    #
    # Narrow windows
    #

    _resizeWatcher = None
    _resizeTarget = None

    def _resizeFilter(self):
        if self._resizeWatcher is None:
            self._resizeWatcher = _ResizeWatcher(self)
        return self._resizeWatcher

    def _panelDock(self):
        return self.shell.mainWindow.findChild("QDockWidget", "PanelDockWidget")

    def updateNarrowMode(self):
        """Narrow mode below ``shell.NARROW_WINDOW_WIDTH``, normal again at ``NARROW_EXIT_WIDTH`` and more."""
        from . import shell as shellModule

        width = self.shell.mainWindow.width
        if width < getattr(shellModule, "NARROW_WINDOW_WIDTH", 1100):
            self.setNarrow(True)
        elif width >= NARROW_EXIT_WIDTH:
            self.setNarrow(False)

    def isNarrow(self):
        return self._narrow

    def setNarrow(self, narrow):
        """In a narrow window the inspector may be as narrow as ``shell.MIN_PANEL_WIDTH`` (its contents scroll)."""
        from . import shell as shellModule

        narrow = bool(narrow)
        if narrow == self._narrow:
            return
        self._narrow = narrow
        dock = self._panelDock()
        if dock is None:
            return
        minimumWidth = getattr(shellModule, "MIN_PANEL_WIDTH", 300)
        if narrow:
            if dock.minimumWidth > minimumWidth:
                self._panelMinimumWidth = dock.minimumWidth
                dock.setMinimumWidth(minimumWidth)
        elif self._panelMinimumWidth is not None:
            if dock.minimumWidth == minimumWidth:
                dock.setMinimumWidth(self._panelMinimumWidth)
            self._panelMinimumWidth = None


class _ResizeWatcher(qt.QObject):
    """Resize events of the top bar, which is as wide as the main window (narrow mode)."""

    def __init__(self, owner):
        qt.QObject.__init__(self)
        self.owner = owner

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize:
            self.owner.updateNarrowMode()
        return False


def _isAlive(widget):
    if widget is None:
        return False
    try:
        widget.objectName
    except Exception:
        return False
    return True


def install(shell):
    """Studio plug-in entry point (``shell.OPTIONAL_PLUGINS``)."""
    global _instance
    if _instance is not None:
        uninstall(_instance.shell)
    _instance = Accessibility(shell)
    _instance.install()


def uninstall(shell):
    global _instance
    if _instance is None:
        return
    try:
        _instance.uninstall()
    finally:
        _instance = None

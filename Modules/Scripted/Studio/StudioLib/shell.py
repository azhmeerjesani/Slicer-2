r"""Shell of Slicer Studio, the streamlined default user interface (SPEC 2).

On a normal launch, ``onStartupCompleted()`` applies Studio to the main window: one 44 px top bar
replaces the menubar, the toolbars and the status bar, and the module panel becomes a 340 px
inspector on the right with a 40 px header. Workspaces (Library, Draw, Map) are modules shown in
the inspector. Nothing is deleted or renamed: the Classic interface is restored exactly by
``Shell.classic()`` (``Ctrl+Alt+0``).

Studio never runs under ``--testing`` or ``--no-main-window``, nor with ``SLICER_CLASSIC_UI=1`` or
``Studio/Enabled=false``. ``instance()`` returns None in these cases and in Classic: every consumer
must handle None.

Public API (SPEC 2.10): ``instance()``, ``willApplyAtStartup()``, ``Shell.switchTo()``,
``currentWorkspace()``, ``toast()``, ``recentMessages()``, ``setBreadcrumb()``, ``setStatusChip()``,
``setSaveStatus()``, ``setSaveStatusMenu()`` (the autosave status of the top bar, A-01b),
``setInspectorFooter()``, ``setInspectorNotice()``, ``clearInspectorNotice()``, ``setCanvas()``,
``setInspectorVisible()``, ``toggleInspector()``, ``setFocusMode()`` (named "Hide panels" in the UI),
``handleEscape()``, ``registerEscapeOverlay()``, ``unregisterEscapeOverlay()``, ``openPalette()``,
``setPrimaryAction()``, ``addWorkspaceShortcut()``, ``studySession``, ``classic()``, ``studio()`` and
``panelWidth``.

Navigation ("where am I, how do I get back", USABILITY 2-3): the tab or the shortcut of the current
workspace shows the inspector again and the hidden panels; ``Ctrl+\`` or ``Ctrl+5`` shows or hides the
inspector (Tab and Shift+Tab keep moving the keyboard focus) and a hidden inspector has an edge handle;
hidden panels show a "Show panels" pill; a transient module has a labelled "← <workspace>" back button;
Esc backs out of one state at a time (``ESCAPE_LADDER``, ``Shell.handleEscape()``); Classic -> Studio
returns to the workspace of the current module.

Keyboard (USABILITY 9): F10 opens the ≡ menu (also Alt, and Alt+F/E/V/H for its submenus, on Windows and
Linux); F6 and Shift+F6 move the focus between the top bar, the inspector and the views (or the Library
canvas); Space and Enter press a focused button of the top bar and of the panel header.

Studio also turns on the Studio-only navigation behaviours of the views while it is shown (left-drag
window/level, PageUp/PageDown by ``Studio/PageStepSlices``, clamped slice stepping, symmetric zoom and
focus follows the mouse) and turns them off in Classic.

Settings are stored under ``Studio/*`` only. The shell does not change values of keys that other
Slicer installations read (SPEC 9.7 rule 5), except that after the application window is closed it
replaces what Slicer saved there while Studio was shown by the Classic equivalent: the classic
window state in ``MainWindow/windowState`` and a portable layout in ``MainWindow/layout``.
"""

import collections
import importlib
import logging
import os
import sys
import time
import traceback
from html import escape as _escapeHtml

import qt
import slicer
from slicer.i18n import tr as _

from . import hud, layouts, topbar, ui, workspaces

#: Plug-ins installed when Studio is applied (``install(shell)``) and removed in Classic
#: (``uninstall(shell)``). Missing modules are skipped.
OPTIONAL_PLUGINS = ["StudioLib.importer", "StudioLib.probe", "StudioLib.journal", "StudioLib.study_ui", "StudioLib.orientation", "StudioLib.finder",
                    "StudioLib.modes", "StudioLib.a11y", "StudioLib.firstrun", "StudioLib.tour", "StudioLib.autosave_ui", "StudioLib.projects_ui",
                    "StudioLib.templates_ui"]

DEFAULT_PANEL_WIDTH = 340
MIN_PANEL_WIDTH = 300
MAX_PANEL_WIDTH = 480
PANEL_HEADER_HEIGHT = 40
PANEL_HEADER_BUTTON_SIZE = 26
#: Help popover of the panel header: a card under the header, as wide as the inspector
HELP_POPOVER_PADDING = 12
HELP_POPOVER_MIN_WIDTH = 240
HELP_POPOVER_MIN_HEIGHT = 200
HELP_POPOVER_MAX_HEIGHT = 420

#: Shows or hides the inspector. Not Tab: Tab and Shift+Tab move the keyboard focus, also out of the views.
INSPECTOR_SHORTCUT = "Ctrl+\\"
#: Second key of the inspector, for keyboard layouts where Ctrl+\ needs AltGr (German, French, Spanish, Nordic):
#: the classic View > Module Panel key (the shortcut of the dock widget's toggle view action, not a Studio shortcut)
INSPECTOR_ALTERNATE_SHORTCUT = "Ctrl+5"
#: "Hide panels" (formerly "focus mode"): F11, which macOS reserves (Show Desktop), and Ctrl+6 (⌘6)
FOCUS_MODE_SHORTCUTS = ("F11", "Ctrl+6")
#: Cycle the keyboard focus between the regions of the window: top bar, inspector, views (or the Library canvas)
FOCUS_REGION_SHORTCUTS = ("F6", "Shift+F6")
#: Opens the ≡ menu on every platform
APP_MENU_SHORTCUT = "F10"
#: Windows and Linux only (on macOS Option+letter types characters): open ≡ and one of its submenus. Not Alt+V:
#: the slice views use V with any modifier (SPEC 9.6); Alt and then V opens ≡ > View (its mnemonic) instead.
APP_SUBMENU_SHORTCUTS = (("Alt+F", "FileMenu"), ("Alt+E", "EditMenu"), ("Alt+H", "HelpMenu"))

#: The Esc ladder (USABILITY 3), one rung per key press, as (state, what Esc does), for the shortcut list.
#: Before these, Qt popups and dialogs close themselves and text fields with unsent edits, consoles and open
#: item editors keep their Esc. Library is outside the ladder: it is a workspace, not a state (its visible
#: back is "Continue in Draw" on the Current work card).
ESCAPE_LADDER = [
    (_("Popover, search or shortcut list"), _("close it")),
    (_("Place / window-level mode"), _("back to navigation")),
    (_("Maximized or single view"), _("restore the layout")),
    (_("Hidden panels"), _("show them")),
    (_("Active Draw tool"), _("deselect")),
    (_("Module from Search"), _("back to the workspace")),
]

#: Studio-only navigation behaviours of the views (SPEC 6.2, USABILITY 9.1): (settings key, default)
LEFT_DRAG_WINDOW_LEVEL_KEY = ("Studio/LeftDragWindowLevel", True)
PAGE_STEP_SLICES_KEY = ("Studio/PageStepSlices", 10)
FOCUS_FOLLOWS_MOUSE_KEY = ("Studio/FocusFollowsMouse", True)
#: Application property read by qMRMLSliceView::enterEvent (W3-02a)
FOCUS_FOLLOWS_MOUSE_PROPERTY = "SlicerFocusFollowsMouse"

#: Edge handle of a hidden inspector (USABILITY 6.3, "Panel handle" slot)
INSPECTOR_HANDLE_SIZE = (20, 56)
#: A consequence toast stays until the next input, but at least this long
STICKY_TOAST_MINIMUM_MS = 4000
#: Number of toast messages kept for "Recent messages"
RECENT_MESSAGES_COUNT = 20
#: Link of the hook help popover that shows the module documentation instead
_MODULE_HELP_LINK = "studio-help:module"
#: Prefix of the links that plug-ins add to the help popover (``Shell.addHelpLink()``)
_HELP_LINK_PREFIX = "studio-help:link:"

#: Explicit minimum width of the module panel in Studio. The minimum size hint of the module panel is the
#: width of the module widget, so a wide tool (for example the Paint options) would widen the inspector and
#: move the views under the cursor: the inspector keeps its width and wide contents scroll instead.
_MODULE_PANEL_MINIMUM_WIDTH = 240
#: Widest inspector that a workspace module may ask for (its layout minimum). Wider contents scroll.
MAX_WORKSPACE_PANEL_WIDTH = 600
#: Widest layout minimum seen so far for each workspace module, kept across sessions so that the inspector
#: does not widen again when a tool is chosen: "Studio/WorkspacePanelWidths/<module name>" holds
#: "<width>@<layout signature>" (see ``Shell._layoutSignature()``)
_WORKSPACE_PANEL_WIDTHS_GROUP = "Studio/WorkspacePanelWidths"

#: Part of the available screen area given to the main window when Slicer did not restore its geometry
FIRST_RUN_WINDOW_FRACTION = 0.9
#: Narrow mode of the top bar (SPEC 4.5, U-03): below this main window width (logical px) the search pill is an
#: icon and the switcher shows icons only (``TopBar.setCompact``), so that the bar fits
NARROW_WINDOW_WIDTH = 1100

#: Main window actions that keep their classic behavior in Studio: they are not attached to the
#: main window (Ctrl+Z/Y are owned by the Segment Editor, Ctrl+6 by "Hide panels", Ctrl+O, Ctrl+F and Ctrl+H
#: by Studio)
_KEEP_UNATTACHED_ACTIONS = ("EditUndoAction", "EditRedoAction", "WindowMaximizeViewAction", "FileAddDataAction",
                            "ViewFindModuleAction", "ModuleHomeAction")

_PORTABLE_LAYOUT = slicer.vtkMRMLLayoutNode.SlicerLayoutConventionalView

_shell = None
_closeGuard = None


def instance():
    """Return the live Studio shell, or None (Classic, testing, no main window, Studio disabled)."""
    if _shell is not None and _shell.isActive():
        return _shell
    return None


def _settingsBool(key, default):
    return slicer.util.settingsValue(key, default, converter=slicer.util.toBool)


def _settingsInt(key, default):
    try:
        return int(slicer.util.settingsValue(key, default, converter=int))
    except (TypeError, ValueError):
        return default


def willApplyAtStartup():
    """True if ``onStartupCompleted()`` will apply Studio: not testing, a main window, ``SLICER_CLASSIC_UI`` is
    not "1", ``Studio/Enabled`` and ``Studio/Mode`` is not "classic". Nothing is changed.
    """
    if slicer.app.testingEnabled() or slicer.app.commandOptions().noMainWindow:
        return False
    if slicer.util.mainWindow() is None:
        return False
    if os.environ.get("SLICER_CLASSIC_UI") == "1":
        return False
    if not _settingsBool("Studio/Enabled", True):
        return False
    return str(slicer.util.settingsValue("Studio/Mode", "studio")).lower() != "classic"


def isMacOS():
    return sys.platform == "darwin"


def focusModeKeyText():
    """The key of "Hide panels" as displayed on this platform: ⌘6 on macOS (F11 is Show Desktop there), else F11."""
    key = FOCUS_MODE_SHORTCUTS[1] if isMacOS() else FOCUS_MODE_SHORTCUTS[0]
    return qt.QKeySequence(key).toString(qt.QKeySequence.NativeText)


def _execMenu(menu, point):
    """Show ``menu`` at ``point`` and wait until it is closed (QMenu.exec in Qt 6, exec_ in Qt 5)."""
    run = getattr(menu, "exec", None) or menu.exec_
    return run(point)


def _ancestors(widget):
    """``widget`` and its parent widgets."""
    while widget is not None:
        yield widget
        widget = widget.parentWidget()


def _isEditingItemView(view):
    """True if an item editor of ``view`` (a QAbstractItemView) is open."""
    state = getattr(view, "state", None)
    if callable(state):
        try:
            return state() == qt.QAbstractItemView.EditingState
        except Exception:
            pass
    return False


def _keepsEscape(focusWidget):
    """True if the focus widget uses Esc itself, so that the Esc ladder does not run (USABILITY 3, rung 0).

    Consoles, editable multi-line text fields, open item editors and line edits with unsent edits keep Esc.
    A line edit of a spin box or of a combo box counts as its parent: spin boxes and closed combo boxes do
    not keep Esc (see ``_leavesFieldOnEscape()``).
    """
    if focusWidget is None:
        return False
    for ancestor in _ancestors(focusWidget):
        if ancestor.inherits("ctkConsole"):
            return True
        if ancestor.inherits("QAbstractItemView"):
            # The focus widget is the view itself, or an editor in its viewport
            if ancestor == focusWidget:
                return _isEditingItemView(ancestor)
            return _isEditingItemView(ancestor) or any(focusWidget.inherits(name) for name in ("QLineEdit", "QAbstractSpinBox", "QComboBox", "QTextEdit"))
    if focusWidget.inherits("QTextEdit") or focusWidget.inherits("QPlainTextEdit"):
        return not focusWidget.readOnly
    if focusWidget.inherits("QLineEdit"):
        parent = focusWidget.parentWidget()
        if parent is not None and (parent.inherits("QAbstractSpinBox") or parent.inherits("QComboBox")):
            return False
        return bool(focusWidget.isModified()) and bool(focusWidget.text)
    return False


def _isTextInput(widget):
    """True if ``widget`` takes typed text: an editable line edit, text field, spin box or combo box."""
    if widget is None:
        return False
    if widget.inherits("QLineEdit") or widget.inherits("QTextEdit") or widget.inherits("QPlainTextEdit"):
        return not widget.readOnly
    if widget.inherits("QAbstractSpinBox"):
        return not widget.readOnly
    if widget.inherits("QComboBox"):
        return bool(widget.editable)
    return False


_VIEW_CLASSES = ("ctkVTKAbstractView", "qMRMLSliceView", "qMRMLThreeDView", "qMRMLPlotView")
_RENDER_WIDGET_CLASSES = ("QVTKOpenGLNativeWidget", "QVTKOpenGLStereoWidget", "QVTKOpenGLWidget", "QVTKRenderWidget")


def _isInView(widget):
    """True if ``widget`` is a view (slice, 3D, plot) or is inside one, such as its render widget."""
    if widget is None:
        return False
    if any(widget.inherits(className) for className in _RENDER_WIDGET_CLASSES):
        return True
    while widget is not None:
        if any(widget.inherits(className) for className in _VIEW_CLASSES):
            return True
        widget = widget.parentWidget()
    return False


def _leavesFieldOnEscape(focusWidget):
    """True for a spin box or a closed combo box (or their line edit): Esc moves the focus out of it and runs the ladder."""
    if focusWidget is None:
        return False
    widget = focusWidget
    if widget.inherits("QLineEdit") and widget.parentWidget() is not None:
        widget = widget.parentWidget()
    return widget.inherits("QAbstractSpinBox") or widget.inherits("QComboBox")


def associatedWidgets(action):
    """Widgets that ``action`` was added to (Qt 5 has associatedWidgets(), Qt 6 only associatedObjects())."""
    if hasattr(action, "associatedWidgets"):
        return list(action.associatedWidgets())
    # Not isinstance(obj, qt.QWidget): PythonQt wrappers do not inherit from the wrappers of their C++ base classes,
    # so that is False for a QMenu, a QToolBar or the main window, and the list would always be empty
    return [obj for obj in action.associatedObjects() if obj.isWidgetType()]


def onStartupCompleted(startTime=None):
    """Apply Studio after the application started (connected by the Studio module).

    ``startTime`` (``time.perf_counter()``) is the start of the apply time measurement.
    """
    if startTime is None:
        startTime = time.perf_counter()
    if slicer.app.testingEnabled() or slicer.app.commandOptions().noMainWindow:
        return
    mainWindow = slicer.util.mainWindow()
    if mainWindow is None:
        return
    installCloseGuard(mainWindow)
    if os.environ.get("SLICER_CLASSIC_UI") == "1":
        return
    if not _settingsBool("Studio/Enabled", True):
        return
    shell = _shell if _shell is not None else Shell()
    shell.installClassicSwitch()
    if str(slicer.util.settingsValue("Studio/Mode", "studio")).lower() == "classic":
        return
    fitWindowToScreen(mainWindow)
    shell.apply(startTime=startTime)


def fitWindowToScreen(mainWindow, fraction=FIRST_RUN_WINDOW_FRACTION):
    """Give the main window most of the screen when Slicer did not restore a saved geometry.

    Without a saved geometry (first run, or ``MainWindow/RestoreGeometry`` off) the window opens at the
    835x570 px size of its form, too small for the inspector and the views. The settings are only read.
    Return True if the window was resized.
    """
    settings = slicer.app.userSettings()
    restored = slicer.util.toBool(settings.value("MainWindow/RestoreGeometry", False)) and settings.contains("MainWindow/geometry")
    if restored or mainWindow.isMaximized() or mainWindow.isFullScreen():
        return False
    screen = ui.screenOf(mainWindow)
    if screen is None:
        return False
    available = screen.availableGeometry
    if callable(available):
        # A method when the property is not wrapped as an attribute
        available = available()
    width = int(available.width() * fraction)
    height = int(available.height() * fraction)
    if mainWindow.width >= width and mainWindow.height >= height:
        return False
    # The window is already shown (startupCompleted): without updates, the views are not painted at once at the
    # new size during the resize (each OpenGL view renders on resize), only once afterwards (-0.15 s at startup)
    updatesEnabled = mainWindow.updatesEnabled
    if callable(updatesEnabled):
        # A method when the property is not wrapped as an attribute
        updatesEnabled = updatesEnabled()
    mainWindow.setUpdatesEnabled(False)
    try:
        mainWindow.resize(width, height)
    finally:
        mainWindow.setUpdatesEnabled(updatesEnabled)
    mainWindow.move(available.x() + (available.width() - width) // 2, available.y() + (available.height() - height) // 2)
    return True


#
# Saved window state guard
#


class _LabelTextToolTipFilter(qt.QObject):
    """Show a label's whole text as its tooltip (for labels that may be cut)."""

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.ToolTip and hasattr(obj, "text"):
            text = obj.text
            if text and obj.toolTip != text:
                obj.toolTip = text
        return False


class MainWindowCloseGuard(qt.QObject):
    """Keep the settings that Slicer saves when the main window is closed readable by Classic and
    by other Slicer installations (they share the settings file).

    ``qSlicerMainWindow::closeEvent`` asks for confirmation (possibly in modal dialogs), then saves
    ``MainWindow/windowState`` and ``MainWindow/layout`` and the window is hidden. On that Hide event,
    the guard replaces the window state saved while Studio was shown by the classic window state,
    and a fork layout (IDs 7001-7099, unknown to other Slicer versions) by the conventional layout.
    """

    def __init__(self, mainWindow):
        qt.QObject.__init__(self)
        self.mainWindow = mainWindow
        #: Function that returns the classic window state to save, or None (Studio not shown)
        self.classicStateProvider = None
        self._closing = False
        #: MainWindow/windowState when the close started: Slicer saved the state if it changed since
        self._stateAtClose = None
        mainWindow.installEventFilter(self)
        slicer.app.connect("aboutToQuit()", self._onAboutToQuit)

    def eventFilter(self, obj, event):
        if obj != self.mainWindow:
            return False
        eventType = event.type()
        if eventType == qt.QEvent.Close:
            self._closing = True
            self._stateAtClose = qt.QSettings().value("MainWindow/windowState")
            qt.QTimer.singleShot(0, self._checkCloseCancelled)
        elif eventType == qt.QEvent.Hide and self._closing:
            # The close was accepted: the window state and the layout have just been saved
            self._closing = False
            self._fixSavedState()
        return False

    def _checkCloseCancelled(self):
        if not self._closing:
            return
        if slicer.app.activeModalWidget() is not None:
            # Still asking for confirmation (the timer runs in the event loop of the dialog)
            qt.QTimer.singleShot(100, self._checkCloseCancelled)
            return
        if self.mainWindow.isVisible():
            self._closing = False

    def _onAboutToQuit(self):
        """A close accepted without a Hide event (a hidden window): fix what Slicer saved.

        The application can also quit while the exit confirmation is still open (``slicer.app.exit()``
        from a script or a signal): Slicer saved nothing then, so neither does the guard. The window state is
        replaced only if Slicer saved one since the close started; a fork layout is always made portable.
        """
        if not self._closing:
            return
        self._closing = False
        if slicer.app.activeModalWidget() is not None:
            return
        self._fixSavedState(windowState=qt.QSettings().value("MainWindow/windowState") != self._stateAtClose)

    def _fixSavedState(self, windowState=True):
        settings = qt.QSettings()
        if not slicer.util.toBool(settings.value("MainWindow/RestoreGeometry", False)):
            # Slicer did not save the window state and layout
            return
        classicState = self.classicStateProvider() if windowState and self.classicStateProvider is not None else None
        if classicState is not None:
            settings.setValue("MainWindow/windowState", classicState)
        try:
            layoutId = int(settings.value("MainWindow/layout", 0))
        except (TypeError, ValueError):
            layoutId = 0
        if layouts.isStudioLayout(layoutId):
            settings.setValue("MainWindow/layout", _PORTABLE_LAYOUT)
        settings.sync()


def installCloseGuard(mainWindow):
    global _closeGuard
    if _closeGuard is None:
        _closeGuard = MainWindowCloseGuard(mainWindow)
    return _closeGuard


#
# Panel header
#


class PanelHeader:
    """The 40 px header of the inspector, which replaces the module panel title bar (SPEC 2.5).

    In a workspace it shows the workspace title and its caption ("Draw · Segment Editor"). For a transient
    module it shows a labelled back button "← Draw" (objectName ``StudioPanelCloseButton``) and the module
    title. "?" shows the help of the current tool (the workspace hook ``helpHtml(shell)``) or of the module,
    and "⋯" the hook items (``moreMenuActions(shell)``) and the panel actions. The buttons take the keyboard
    focus with Tab; Space and Enter press them.
    """

    def __init__(self, shell):
        self.shell = shell
        self.widget = qt.QFrame()
        self.widget.setObjectName("StudioPanelHeader")
        self.widget.setFixedHeight(PANEL_HEADER_HEIGHT)
        layout = qt.QHBoxLayout(self.widget)
        self._layout = layout
        # QDockWidget reserves the size hint height of a title bar widget (not its fixed height):
        # the 26 px buttons and these margins make the size hint 40 px
        self._verticalMargin = (PANEL_HEADER_HEIGHT - PANEL_HEADER_BUTTON_SIZE) // 2
        layout.setContentsMargins(12, self._verticalMargin, 6, self._verticalMargin)
        layout.setSpacing(2)

        # Back to the workspace that the transient module was opened from (the former close button)
        self.closeButton = self._button("StudioPanelCloseButton", "arrow-left", _("Back"))
        self.closeButton.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        # Labelled: as wide as its text
        self.closeButton.setMaximumSize(16777215, 16777215)
        self.closeButton.setFixedHeight(PANEL_HEADER_BUTTON_SIZE)
        self.closeButton.setMinimumWidth(PANEL_HEADER_BUTTON_SIZE)
        ui._font(self.closeButton, 13)
        self.closeButton.connect("clicked()", self.shell.closeTransientModule)
        self.closeButton.hide()
        layout.addWidget(self.closeButton)
        layout.addSpacing(4)

        # Kept for compatibility, no longer shown (the back button says where the module was opened from)
        self.prefixLabel = qt.QLabel(_("Module ·"))
        self.prefixLabel.setObjectName("StudioPanelHeaderPrefix")
        ui._font(self.prefixLabel, 13)
        self.prefixLabel.hide()
        layout.addWidget(self.prefixLabel)
        self.titleLabel = qt.QLabel()
        self.titleLabel.setObjectName("StudioPanelHeaderTitle")
        ui._font(self.titleLabel, 15, qt.QFont.DemiBold)
        layout.addWidget(self.titleLabel)
        self.captionLabel = qt.QLabel()
        self.captionLabel.setObjectName("StudioPanelHeaderCaption")
        ui._font(self.captionLabel, 13)
        # Never wider than the room left: the header must not widen the inspector
        self.captionLabel.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
        self.captionLabel.hide()
        layout.addSpacing(6)
        layout.addWidget(self.captionLabel, 1)

        self.helpButton = self._button("StudioPanelHelpButton", "circle-help", _("Help and acknowledgment"))
        self.helpButton.connect("clicked()", self.showHelp)
        layout.addWidget(self.helpButton)

        self.moreButton = self._button("StudioPanelMoreButton", "ellipsis", _("More"))
        self.moreButton.setPopupMode(qt.QToolButton.InstantPopup)
        self.moreMenu = qt.QMenu(self.moreButton)
        self.moreMenu.setObjectName("StudioPanelMoreMenu")
        self.dataProbeAction = self.moreMenu.addAction(_("Show Data Probe"))
        self.dataProbeAction.setCheckable(True)
        self.dataProbeAction.connect("toggled(bool)", self.shell.setDataProbeVisible)
        self.moreMenu.addAction(_("Reset panel width")).connect("triggered()", self.shell.resetPanelWidth)
        self.moreMenu.addSeparator()
        self.moreMenu.addAction(_("Open in Classic")).connect("triggered()", self.shell.classic)
        self.moreMenu.connect("aboutToShow()", self._updateMoreMenu)
        self.moreButton.setMenu(self.moreMenu)
        layout.addWidget(self.moreButton)
        #: Actions added by the hook ``moreMenuActions(shell)`` (rebuilt each time the menu is shown)
        self._hookActions = []
        #: Text browser of the help popover while it exists, and the popover while it is shown
        self._helpBrowser = None
        self._helpMenu = None

        ui.bindStyleSheet(self.widget, lambda: (
            f"QFrame#StudioPanelHeader {{ background-color: {ui.token('window').name()}; border: none; border-bottom: 1px solid {ui.token('hairline').name()}; }}"
            f"QLabel#StudioPanelHeaderPrefix {{ color: {ui.token('text2').name()}; }}"
            f"QLabel#StudioPanelHeaderCaption {{ color: {ui.token('text2').name()}; }}"))
        self.refreshIcons()
        ui.addPaletteListener(self.refreshIcons)

    @staticmethod
    def _button(objectName, iconName, toolTip):
        button = qt.QToolButton()
        button.setObjectName(objectName)
        button.setToolTip(toolTip)
        button.setAutoRaise(True)
        # Reachable with Tab (not by a click: the focus stays where the user works)
        button.setFocusPolicy(qt.Qt.TabFocus)
        button.setIconSize(qt.QSize(16, 16))
        button.setFixedSize(PANEL_HEADER_BUTTON_SIZE, PANEL_HEADER_BUTTON_SIZE)
        button.setProperty("studioIcon", iconName)
        ui.setRole(button, "ghost")
        ui.installButtonKeys(button)
        return button

    def refreshIcons(self):
        for button in (self.helpButton, self.moreButton, self.closeButton):
            button.setIcon(ui.icon(button.property("studioIcon"), 16))

    def setTitle(self, title, transient=False, closeToolTip=None, backText=None, caption=None):
        """Show a workspace title (with its ``caption``), or with ``transient`` a module title after the back button.

        ``backText`` is the label of the back button (the workspace the module was opened from) and
        ``closeToolTip`` its tooltip.
        """
        self.titleLabel.setText(title)
        # The "Module ·" prefix is replaced by the labelled back button
        self.prefixLabel.setVisible(False)
        self.closeButton.setText(backText or "")
        self.closeButton.setVisible(transient)
        if closeToolTip:
            self.closeButton.setToolTip(closeToolTip)
        captionText = f"· {caption}" if caption and not transient else ""
        self.captionLabel.setText(captionText)
        self.captionLabel.setVisible(bool(captionText))
        self._layout.setContentsMargins(6 if transient else 12, self._verticalMargin, 6, self._verticalMargin)

    def _currentHooksWorkspace(self):
        return workspaces.workspace(self.shell.currentWorkspace())

    def _updateMoreMenu(self):
        dataProbe = self.shell.mainWindow.findChild("QWidget", "DataProbeCollapsibleWidget")
        self.dataProbeAction.blockSignals(True)
        self.dataProbeAction.setChecked(dataProbe is not None and not dataProbe.isHidden())
        self.dataProbeAction.blockSignals(False)
        # Items of the workspace hooks, above the panel items
        for action in self._hookActions:
            self.moreMenu.removeAction(action)
            submenu = action.menu()
            (submenu if submenu is not None else action).deleteLater()
        self._hookActions = []
        items = workspaces.callHook(self._currentHooksWorkspace(), "moreMenuActions", self.shell) or []
        for item in items:
            if item is None:
                # A separator between groups of hook items
                if self._hookActions and not self._hookActions[-1].isSeparator():
                    separator = qt.QAction(self.moreMenu)
                    separator.setSeparator(True)
                    self.moreMenu.insertAction(self.dataProbeAction, separator)
                    self._hookActions.append(separator)
                continue
            try:
                text, callback, checked = (tuple(item) + (None, None))[:3]
            except TypeError:
                continue
            if isinstance(callback, (list, tuple)):
                # A submenu: callback is a list of (text, callback, checked) items
                action = self._hookSubmenu(text, callback).menuAction()
            else:
                action = qt.QAction(text, self.moreMenu)
                if checked is not None:
                    action.setCheckable(True)
                    action.setChecked(bool(checked))
                if callback is not None:
                    action.connect("triggered()", callback)
            action.setObjectName("StudioPanelHookAction")
            self.moreMenu.insertAction(self.dataProbeAction, action)
            self._hookActions.append(action)
        while self._hookActions and self._hookActions[-1].isSeparator():
            self.moreMenu.removeAction(self._hookActions[-1])
            self._hookActions.pop().deleteLater()
        if self._hookActions:
            separator = qt.QAction(self.moreMenu)
            separator.setSeparator(True)
            self.moreMenu.insertAction(self.dataProbeAction, separator)
            self._hookActions.append(separator)

    def _hookSubmenu(self, text, items):
        """A submenu of the ⋯ menu for a hook item whose callback is a list of ``(text, callback, checked)`` items."""
        menu = qt.QMenu(text, self.moreMenu)
        menu.setObjectName("StudioPanelHookMenu")
        for item in items:
            if item is None:
                menu.addSeparator()
                continue
            itemText, callback, checked = (tuple(item) + (None, None))[:3]
            action = menu.addAction(itemText)
            if checked is not None:
                action.setCheckable(True)
                action.setChecked(bool(checked))
            if callback is not None:
                action.connect("triggered()", callback)
            else:
                action.setEnabled(False)
        return menu

    def _moduleHelpHtml(self):
        """Help and acknowledgment of the module shown in the inspector, or None."""
        moduleName = self.shell.currentModuleName()
        module = slicer.app.moduleManager().module(moduleName) if moduleName and workspaces.moduleExists(moduleName) else None
        if module is None:
            return None
        html = f'<h3 style="margin-top: 0px;">{module.title}</h3>{module.helpText}'
        if module.acknowledgementText:
            html += f"<h4>{_('Acknowledgment')}</h4>{module.acknowledgementText}"
        return html

    def helpHtml(self):
        """HTML of the help popover: the help of the current tool (workspace hook ``helpHtml(shell)``, followed by
        a "Module documentation" link), else the module help. None if there is nothing to show.
        """
        moduleHtml = self._moduleHelpHtml()
        hookHelp = workspaces.callHook(self._currentHooksWorkspace(), "helpHtml", self.shell)
        if hookHelp:
            try:
                title, body = hookHelp
            except (TypeError, ValueError):
                title, body = None, str(hookHelp)
            text = f'<h3 style="margin-top: 0px;">{title}</h3>{body}' if title else str(body)
            if moduleHtml is not None:
                text += f'<p><a href="{_MODULE_HELP_LINK}">{_("Module documentation")}</a></p>'
            return self._withPluginLinks(text)
        return self._withPluginLinks(moduleHtml)

    def _withPluginLinks(self, text):
        """``text`` followed by the enabled links of the plug-ins (``Shell.addHelpLink()``), or None if both are empty."""
        links = [f'<a href="{_HELP_LINK_PREFIX}{key}">{_escapeHtml(linkText)}</a>' for key, linkText in self.shell.helpLinks()]
        if not links:
            return text
        return (text or "") + f'<p>{" · ".join(links)}</p>'

    def helpPopover(self):
        """Create the help popover of the current tool or module, or return None if there is no help.

        It is a menu that holds a text browser, to be shown under the header (``helpPopoverPosition()``) over the
        whole inspector width. It is a card with a border and padding, so that it reads as a surface of its own
        rather than as text over the inspector.
        """
        html = self.helpHtml()
        if html is None:
            return None
        menu = qt.QMenu(self.helpButton)
        menu.setObjectName("StudioHelpPopover")
        card = ui.token("card").name()
        menu.setStyleSheet(
            f"QMenu#StudioHelpPopover {{ background-color: {card}; border: 1px solid {ui.token('border').name()}; padding: 0px; }}"
            f"QTextBrowser#StudioHelpBrowser {{ background-color: {card}; border: none; }}")
        browser = qt.QTextBrowser()
        browser.setObjectName("StudioHelpBrowser")
        # Links are handled here: the "Module documentation" link shows the module help in the same popover
        browser.setOpenLinks(False)
        # A bound method, not a lambda that holds the browser: the browser would keep itself alive from Python
        self._helpBrowser = browser
        browser.connect("anchorClicked(QUrl)", self._onHelpLinkClicked)
        browser.setFrameShape(qt.QFrame.NoFrame)
        browser.setHtml(html)
        browser.document.setDocumentMargin(HELP_POPOVER_PADDING)
        # As wide as the inspector (inside the 1 px border), as high as the inspector allows
        dock = self.widget.parentWidget()
        available = (dock.height if dock is not None else HELP_POPOVER_MAX_HEIGHT) - self.widget.height - HELP_POPOVER_PADDING
        browser.setFixedSize(max(self.widget.width - 2, HELP_POPOVER_MIN_WIDTH), max(HELP_POPOVER_MIN_HEIGHT, min(HELP_POPOVER_MAX_HEIGHT, available)))
        action = qt.QWidgetAction(menu)
        action.setDefaultWidget(browser)
        menu.addAction(action)
        return menu

    def _onHelpLinkClicked(self, url):
        browser = self._helpBrowser
        if browser is None:
            return
        if url.toString().startswith(_HELP_LINK_PREFIX):
            callback = self.shell.helpLinkCallback(url.toString()[len(_HELP_LINK_PREFIX):])
            if self._helpMenu is not None:
                self._helpMenu.close()
            if callback is not None:
                # After the popover is closed
                qt.QTimer.singleShot(0, callback)
            return
        if url.toString() == _MODULE_HELP_LINK:
            moduleHtml = self._moduleHelpHtml()
            if moduleHtml is not None:
                browser.setHtml(moduleHtml)
            return
        if url.toString().startswith("#"):
            browser.scrollToAnchor(url.fragment())
            return
        qt.QDesktopServices.openUrl(url)

    def helpPopoverPosition(self):
        """Global position of the help popover: the bottom left corner of the header."""
        return self.widget.mapToGlobal(qt.QPoint(0, self.widget.height))

    def showHelp(self):
        menu = self.helpPopover()
        if menu is None:
            return
        self._helpMenu = menu
        _execMenu(menu, self.helpPopoverPosition())
        self._helpMenu = None
        self._helpBrowser = None
        menu.deleteLater()

    def focusTarget(self):
        """The control of the header that the F6 focus regions start from (the back button), or None."""
        return self.closeButton if self.closeButton.isVisible() else None

    def dispose(self):
        ui.removePaletteListener(self.refreshIcons)
        ui.bindStyleSheet(self.widget, None)
        self._helpBrowser = None
        self._helpMenu = None
        self._hookActions = []
        self.widget.hide()
        # Until Qt deletes it, the header must not be found by its object names
        for widget in (self.widget, self.closeButton, self.helpButton, self.moreButton):
            widget.setObjectName("")
        self.widget.deleteLater()


#
# Overlays: consequence toast and inspector edge handle
#


#: Lucide icons of the application settings panels in Studio (V-02), by panel title; other panels get SETTINGS_PANEL_ICON
SETTINGS_PANEL_ICONS = {
    "General": "settings", "Modules": "box", "Appearance": "moon", "Views": "layout-grid", "User": "users", "Extensions": "download",
    "Cache": "archive-restore", "Internationalization": "globe", "Python": "terminal", "Developer": "sliders-horizontal",
    "Extension Wizard": "wand-sparkles", "Subject hierarchy": "list", "Units": "scaling", "Segmentations": "brush",
    "Volume rendering": "box", "DICOM": "image", "Updates": "refresh-cw", "Templates": "brain", "Saving": "folder",
    "Drawing": "pencil", "Keyboard": "keyboard", "Navigation": "mouse-pointer-2",
}
SETTINGS_PANEL_ICON = "sliders-horizontal"
#: Role of the original text of a panel item (the Studio look writes sentence case)
_SETTINGS_TEXT_ROLE = qt.Qt.UserRole + 1101


def _propertyValue(obj, name):
    """A Qt property read the same way whether PythonQt exposes it as a property or as a getter method."""
    value = getattr(obj, name)
    return value() if callable(value) else value


def sentenceCase(text):
    """``text`` in sentence case: the first word as it is, the other words lower case unless they are acronyms ("DICOM")."""
    words = str(text).split(" ")
    return " ".join([words[0]] + [word if len(word) > 1 and word.isupper() else word.lower() for word in words[1:]])


class _SettingsDialogFilter(qt.QObject):
    def __init__(self, look):
        qt.QObject.__init__(self)
        self.look = look

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Show:
            try:
                self.look.apply()
            except Exception:
                logging.error(f"Studio: the settings dialog look failed:\n{traceback.format_exc()}")
        return False


class SettingsDialogLook:
    """The Studio look of the application settings dialog (``ctkSettingsDialog``, VISUAL 4.7, V-02), applied each time it is
    shown while Studio is applied and undone in Classic (``restore()``), which keeps the official look:

    - the panel list: Lucide icons instead of the application icon, sentence-case names (only the shown text: panels are
      looked up by their title), 32 px rows with the ``selected`` fill;
    - the footer: flat buttons without icons, "Restore defaults" and "Reset" as ghost buttons on the left, Cancel secondary
      and OK primary;
    - 16 px margins around the dialog.
    """

    def __init__(self, dialog):
        self.dialog = dialog
        self.applied = False
        self._saved = {}
        self._filter = _SettingsDialogFilter(self)
        dialog.installEventFilter(self._filter)
        if dialog.isVisible():
            self.apply()

    def _tree(self):
        return self.dialog.findChild("QTreeWidget", "SettingsTreeWidget")

    def _buttonBox(self):
        return self.dialog.findChild("QDialogButtonBox", "SettingsButtonBox")

    @staticmethod
    def _items(tree):
        items = []

        def visit(item):
            for index in range(item.childCount()):
                child = item.child(index)
                items.append(child)
                visit(child)

        visit(tree.invisibleRootItem())
        return items

    def _treeStyleSheet(self):
        t = {name: ui.token(name).name() for name in ("text", "text2", "selected", "control-hover", "accent-fg")}
        if not ui.isDark():
            t["selected"] = ui.token("control-pressed").name()
        return (
            "QTreeWidget#SettingsTreeWidget { background: transparent; border: none; outline: 0; padding: 4px 0px; }"
            f"QTreeWidget#SettingsTreeWidget::item {{ height: 32px; border-radius: 8px; padding-left: 6px; color: {t['text2']}; }}"
            f"QTreeWidget#SettingsTreeWidget::item:hover {{ background-color: {t['control-hover']}; color: {t['text']}; }}"
            f"QTreeWidget#SettingsTreeWidget::item:selected {{ background-color: {t['selected']}; color: {t['text']}; }}"
            "QTreeWidget#SettingsTreeWidget::branch { image: none; border: none; background: transparent; }")

    def apply(self):
        dialog = self.dialog
        if not ui._isAlive(dialog):
            return
        tree = self._tree()
        if tree is not None:
            if not self.applied:
                self._saved["tree"] = (tree.styleSheet, tree.indentation, tree.iconSize, tree.minimumWidth)
            for item in self._items(tree):
                original = item.data(0, _SETTINGS_TEXT_ROLE)
                if original is None or original == "":
                    original = item.text(0)
                    item.setData(0, _SETTINGS_TEXT_ROLE, original)
                    self._saved.setdefault("icons", {})[original] = item.icon(0)
                item.setText(0, sentenceCase(original))
                item.setIcon(0, ui.icon(SETTINGS_PANEL_ICONS.get(str(original), SETTINGS_PANEL_ICON), 16))
            tree.setIndentation(0)
            tree.setIconSize(qt.QSize(16, 16))
            tree.setMinimumWidth(200)
            ui.bindStyleSheet(tree, self._treeStyleSheet)
        box = self._buttonBox()
        if box is not None:
            roles = ((qt.QDialogButtonBox.Ok, "primary", None), (qt.QDialogButtonBox.Cancel, "secondary", None),
                     (qt.QDialogButtonBox.RestoreDefaults, "ghost", _("Restore defaults")), (qt.QDialogButtonBox.Reset, "ghost", None))
            for standard, role, text in roles:
                button = box.button(standard)
                if button is None:
                    continue
                key = f"button{int(standard)}"
                if key not in self._saved:
                    self._saved[key] = (button.icon, button.text, button.property("slicerRole"))
                button.setIcon(qt.QIcon())
                if text:
                    button.setText(text)
                button.setMinimumHeight(ui.BUTTON_HEIGHT)
                ui.setRole(button, role)
        self._applyAppearance()
        layout = dialog.layout()
        if layout is not None:
            if "margins" not in self._saved:
                margins = layout.contentsMargins()
                self._saved["margins"] = (margins.left(), margins.top(), margins.right(), margins.bottom(), _propertyValue(layout, "horizontalSpacing"),
                                          _propertyValue(layout, "verticalSpacing"))
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setHorizontalSpacing(20)
            layout.setVerticalSpacing(12)
        self.applied = True

    #: Labels of the Appearance panel (``qSlicerSettingsStylesPanel``) and its check boxes, drawn as switches in Studio
    APPEARANCE_LABELS = ("ThemeLabel", "AccentColorLabel", "DensityLabel", "FontLabel", "ShowToolTipsLabel", "ShowToolButtonTextLabel", "RestoreUILabel")
    APPEARANCE_CHECKBOXES = ("ShowToolTipsCheckBox", "ShowToolButtonTextCheckBox", "RestoreUICheckBox")

    def _appearancePanel(self):
        return self.dialog.findChild("QWidget", "qSlicerSettingsStylesPanel")

    def _applyAppearance(self):
        """The Appearance panel as a Studio page (VISUAL 4.7): the page title and one line, labels without colons on 44 px
        rows, switches for the check boxes. Undone by ``_restoreAppearance()``.
        """
        panel = self._appearancePanel()
        form = panel.layout() if panel is not None else None
        if form is None or "appearance" in self._saved:
            return
        saved = {"labels": {}, "margins": None, "spacing": _propertyValue(form, "verticalSpacing"), "header": None}
        margins = form.contentsMargins()
        saved["margins"] = (margins.left(), margins.top(), margins.right(), margins.bottom())
        for name in self.APPEARANCE_LABELS:
            label = panel.findChild("QLabel", name)
            if label is None:
                continue
            saved["labels"][name] = (label.text, label.minimumHeight)
            label.setText(str(label.text).rstrip(" :"))
            label.setMinimumHeight(ui.SETTINGS_ROW_HEIGHT)
        for name in self.APPEARANCE_CHECKBOXES:
            box = panel.findChild("QCheckBox", name)
            if box is not None:
                box.setProperty("slicerRole", "switch")
                box.style().unpolish(box)
                box.style().polish(box)
        header = qt.QWidget()
        header.setObjectName("StudioAppearancePageHeader")
        headerLayout = qt.QVBoxLayout(header)
        headerLayout.setContentsMargins(0, 0, 0, 8)
        headerLayout.setSpacing(6)
        ui.settingsPageHeader(headerLayout, _("Appearance"), _("Theme, accent color, density and font of Slicer Studio."), "StudioAppearancePage")
        form.insertRow(0, header)
        saved["header"] = header
        form.setContentsMargins(24, 20, 24, 16)
        form.setVerticalSpacing(0)
        self._saved["appearance"] = saved

    def _restoreAppearance(self):
        saved = self._saved.pop("appearance", None)
        panel = self._appearancePanel()
        form = panel.layout() if panel is not None else None
        if saved is None or form is None:
            return
        for name, (text, minimumHeight) in saved["labels"].items():
            label = panel.findChild("QLabel", name)
            if label is not None:
                label.setText(text)
                label.setMinimumHeight(minimumHeight)
        for name in self.APPEARANCE_CHECKBOXES:
            box = panel.findChild("QCheckBox", name)
            if box is not None:
                box.setProperty("slicerRole", "")
                box.style().unpolish(box)
                box.style().polish(box)
        if saved["header"] is not None and ui._isAlive(saved["header"]):
            form.removeRow(saved["header"])
        form.setContentsMargins(*saved["margins"])
        form.setVerticalSpacing(saved["spacing"])

    def restore(self):
        """The official look again (Classic)."""
        dialog = self.dialog
        if not ui._isAlive(dialog):
            return
        dialog.removeEventFilter(self._filter)
        if not self.applied:
            return
        tree = self._tree()
        if tree is not None:
            icons = self._saved.get("icons", {})
            for item in self._items(tree):
                original = item.data(0, _SETTINGS_TEXT_ROLE)
                if original:
                    item.setText(0, original)
                    if original in icons:
                        item.setIcon(0, icons[original])
                    item.setData(0, _SETTINGS_TEXT_ROLE, "")
            ui.bindStyleSheet(tree, None)
            styleSheet, indentation, iconSize, minimumWidth = self._saved.get("tree", ("", 20, qt.QSize(16, 16), 0))
            tree.setStyleSheet(styleSheet)
            tree.setIndentation(indentation)
            tree.setIconSize(iconSize)
            tree.setMinimumWidth(minimumWidth)
        box = self._buttonBox()
        if box is not None:
            for standard in (qt.QDialogButtonBox.Ok, qt.QDialogButtonBox.Cancel, qt.QDialogButtonBox.RestoreDefaults, qt.QDialogButtonBox.Reset):
                button = box.button(standard)
                saved = self._saved.get(f"button{int(standard)}")
                if button is None or saved is None:
                    continue
                icon, text, role = saved
                button.setIcon(icon)
                button.setText(text)
                button.setMinimumHeight(0)
                ui.bindStyleSheet(button, None)
                button.setProperty("slicerRole", role if role else "")
                button.style().unpolish(button)
                button.style().polish(button)
        self._restoreAppearance()
        layout = dialog.layout()
        margins = self._saved.get("margins")
        if layout is not None and margins is not None:
            layout.setContentsMargins(*margins[:4])
            layout.setHorizontalSpacing(margins[4])
            layout.setVerticalSpacing(margins[5])
        self._saved = {}
        self.applied = False


class StickyToast:
    """The toast of consequences of automatic actions (USABILITY 6.3): it stays until the next input in the
    application (a mouse button or a key, at least ``STICKY_TOAST_MINIMUM_MS``) and may carry one action button
    (Undo, Show, Details). With a timeout it is a confirmation that carries an action.

    It is drawn like the plain toast (``hud.Toast``) at the same place, and replaces it.
    """

    def __init__(self, parentWidget):
        self.parentWidget = parentWidget
        self.fullText = ""
        self.frame = qt.QFrame(parentWidget)
        self.frame.setObjectName("StudioStickyToast")
        self.frame.setFixedHeight(hud.TOAST_HEIGHT)
        layout = qt.QHBoxLayout(self.frame)
        layout.setContentsMargins(hud.TOAST_ICON_LEFT, 0, 6, 0)
        layout.setSpacing(hud.TOAST_ICON_GAP)
        self.level = "info"
        self.iconLabel = qt.QLabel()
        self.iconLabel.setObjectName("StudioStickyToastIcon")
        self.iconLabel.setFixedSize(hud.TOAST_ICON_SIZE, hud.TOAST_ICON_SIZE)
        layout.addWidget(self.iconLabel)
        self.label = qt.QLabel()
        self.label.setObjectName("StudioStickyToastText")
        layout.addWidget(self.label)
        self.actionButton = qt.QToolButton()
        self.actionButton.setObjectName("StudioStickyToastAction")
        self.actionButton.setAutoRaise(True)
        self.actionButton.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.actionButton.setFocusPolicy(qt.Qt.TabFocus)
        self.actionButton.connect("clicked()", self._onAction)
        ui.installButtonKeys(self.actionButton)
        self.actionButton.hide()
        layout.addWidget(self.actionButton)
        self.frame.hide()
        ui.bindStyleSheet(self.frame, self._styleSheet)
        self._action = None
        self._sticky = False
        self._inputFilter = _StickyToastInputFilter(self)
        self._filterInstalled = False
        self.minimumTimer = qt.QTimer(self.frame)
        self.minimumTimer.setSingleShot(True)
        self.minimumTimer.connect("timeout()", self._onMinimumElapsed)
        self.hideTimer = qt.QTimer(self.frame)
        self.hideTimer.setSingleShot(True)
        self.hideTimer.connect("timeout()", self.hide)
        # The application event filter must be gone before Python is finalized
        slicer.app.connect("aboutToQuit()", self.hide)

    def _styleSheet(self):
        self.iconLabel.setPixmap(hud.toastIconPixmap(self.level))
        _background, text, _border = hud.toastColors()
        return (
            hud.toastStyleSheet("QFrame#StudioStickyToast")
            + "QLabel#StudioStickyToastIcon { background: transparent; border: none; }"
            f"QLabel#StudioStickyToastText {{ color: {text.name()}; background: transparent; font-size: 13px; }}"
            f"QToolButton#StudioStickyToastAction {{ color: #7aa7ff; background: transparent; border: none; border-radius: 10px;"
            f" padding: 2px 8px; font-size: 13px; font-weight: 600; }}"
            f"QToolButton#StudioStickyToastAction:hover {{ background-color: {ui._rgba(text, 0.12)}; }}")

    def show(self, text, sticky=True, ms=None, action=None, actionText=None, level="info"):
        if level != self.level:
            self.level = level
            self.iconLabel.setPixmap(hud.toastIconPixmap(level))
        self._action = action
        self._sticky = sticky
        self.fullText = text
        self.label.setText(text)
        hasAction = action is not None
        self.actionButton.setText(actionText or _("Show"))
        self.actionButton.setVisible(hasAction)
        # Read by screen readers (QAccessible is not wrapped in every build: the name and staying until the
        # next input are what remains then)
        self.label.setAccessibleName(text)
        self.label.setAccessibleDescription(self.actionButton.text if hasAction else "")
        self.frame.setAccessibleName(text)
        self._removeInputFilter()
        self.hideTimer.stop()
        self.minimumTimer.stop()
        self.updatePosition()
        self.frame.show()
        self.frame.raise_()
        if sticky:
            self.minimumTimer.start(STICKY_TOAST_MINIMUM_MS)
        else:
            self.hideTimer.start(max(0, int(ms or 1600)))
        self._announce()

    def _announce(self):
        accessible = getattr(qt, "QAccessible", None)
        event = getattr(qt, "QAccessibleEvent", None)
        if accessible is None or event is None or not hasattr(accessible, "updateAccessibility"):
            return
        try:
            accessible.updateAccessibility(event(self.label, accessible.Alert))
        except Exception:
            pass

    def _onMinimumElapsed(self):
        if self.frame.isVisible() and self._sticky and not self._filterInstalled:
            slicer.app.installEventFilter(self._inputFilter)
            self._filterInstalled = True

    def _removeInputFilter(self):
        if self._filterInstalled:
            slicer.app.removeEventFilter(self._inputFilter)
            self._filterInstalled = False

    def onInput(self, obj):
        """An input event reached ``obj`` after the minimum time: close the toast, unless it is the action button."""
        try:
            if obj is not None and obj.isWidgetType() and (obj == self.frame or self.frame.isAncestorOf(obj)):
                return
        except Exception:
            pass
        self.hide()

    def _onAction(self):
        action = self._action
        self.hide()
        if action is not None:
            try:
                action()
            except Exception:
                logging.error(f"Studio toast action failed:\n{traceback.format_exc()}")

    def hide(self):
        self._removeInputFilter()
        self.minimumTimer.stop()
        self.hideTimer.stop()
        self.frame.hide()

    def updatePosition(self):
        # The whole text, shortened with an ellipsis when the toast is narrower (beside a figure)
        self.label.setText(self.fullText)
        self.frame.adjustSize()
        preferredWidth = self.frame.sizeHint.width()
        x, y, width = hud.toastGeometry(self.parentWidget, preferredWidth)
        self.label.setText(hud.elidedText(self.label, self.fullText, preferredWidth - width))
        self.frame.resize(width, hud.TOAST_HEIGHT)
        self.frame.move(x, y)

    def isVisible(self):
        return self.frame.isVisible()

    def isSticky(self):
        return self._sticky

    def text(self):
        """The whole text (the label may show it shortened)."""
        return self.fullText

    def raise_(self):
        if self.frame.isVisible():
            self.frame.raise_()

    def dispose(self):
        self.hide()
        slicer.app.disconnect("aboutToQuit()", self.hide)
        ui.bindStyleSheet(self.frame, None)
        self.frame.setObjectName("")
        self.frame.deleteLater()


class _StickyToastInputFilter(qt.QObject):
    """Application event filter, installed only while a consequence toast waits for the next input."""

    _INPUT_EVENTS = (qt.QEvent.MouseButtonPress, qt.QEvent.MouseButtonDblClick, qt.QEvent.KeyPress, qt.QEvent.ShortcutOverride,
                     qt.QEvent.Wheel, qt.QEvent.TouchBegin)

    def __init__(self, toast):
        qt.QObject.__init__(self)
        self.toast = toast

    def eventFilter(self, obj, event):
        if event.type() in self._INPUT_EVENTS:
            try:
                if event.type() in (qt.QEvent.KeyPress, qt.QEvent.ShortcutOverride) and event.key() in (
                        qt.Qt.Key_Shift, qt.Qt.Key_Control, qt.Qt.Key_Alt, qt.Qt.Key_Meta, qt.Qt.Key_AltGr):
                    # A modifier alone is not an input yet
                    return False
                self.toast.onInput(obj)
            except Exception:
                logging.error(f"Studio toast input filter failed:\n{traceback.format_exc()}")
        return False


class InspectorHandle:
    """The edge handle (a chevron) at the right edge of the views while the inspector is hidden (USABILITY 6.3)."""

    def __init__(self, shell, parentWidget):
        self.parentWidget = parentWidget
        width, height = INSPECTOR_HANDLE_SIZE
        self.button = qt.QToolButton(parentWidget)
        self.button.setObjectName("StudioInspectorHandle")
        self.button.setFixedSize(width, height)
        self.button.setIconSize(qt.QSize(14, 14))
        self.button.setFocusPolicy(qt.Qt.TabFocus)
        self.button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.button.setToolTip(ui.tooltip(_("Show inspector"), _("{key} or {alternateKey}").format(
            key=ui.keyText(INSPECTOR_SHORTCUT), alternateKey=ui.keyText(INSPECTOR_ALTERNATE_SHORTCUT)), _("or click the current tab")))
        self.button.setAccessibleName(_("Show inspector"))
        self.button.connect("clicked()", lambda: shell.setInspectorVisible(True))
        ui.installButtonKeys(self.button)
        self.button.hide()
        ui.bindStyleSheet(self.button, self._styleSheet)
        self.refreshIcon()
        ui.addPaletteListener(self.refreshIcon)

    def _styleSheet(self):
        background = ui.token("tooltip")
        text = ui.token("tooltip-text")
        radius = INSPECTOR_HANDLE_SIZE[0] // 2
        return (
            f"QToolButton#StudioInspectorHandle {{ background-color: {ui._rgba(background, 0.7)}; border: 1px solid {ui._rgba(text, 0.16)};"
            f" border-right: none; border-top-left-radius: {radius}px; border-bottom-left-radius: {radius}px; padding: 0px; }}"
            f"QToolButton#StudioInspectorHandle:hover {{ background-color: {ui._rgba(background, 0.95)}; }}"
            f"QToolButton#StudioInspectorHandle:focus {{ border: 2px solid {ui.token('accent-fg').name()}; border-right: none; }}")

    def refreshIcon(self):
        self.button.setIcon(qt.QIcon(ui.pixmap("chevron-left", 14, "tooltip-text")))

    def setVisible(self, visible):
        if visible:
            self._place()
            self.button.show()
            self.button.raise_()
        else:
            self.button.hide()

    def isVisible(self):
        return self.button.isVisible()

    def updatePosition(self):
        if self.button.isVisible():
            self._place()

    def _viewBarBands(self):
        """The vertical bands [(top, bottom)] of the view bars that reach the right edge, in parent coordinates."""
        layoutManager = slicer.app.layoutManager()
        viewport = layoutManager.viewport() if layoutManager is not None else None
        if viewport is None:
            return []
        bands = []
        right = self.parentWidget.width - INSPECTOR_HANDLE_SIZE[0]
        for bar in viewport.findChildren("qMRMLViewControllerBar"):
            try:
                if not bar.isVisible():
                    continue
                topLeft = bar.mapTo(self.parentWidget, qt.QPoint(0, 0))
                if topLeft.x() + bar.width > right:
                    bands.append((topLeft.y(), topLeft.y() + bar.height))
            except Exception:
                # Deleted with its view
                pass
        return bands

    def _place(self):
        """At the right edge, vertically centred, but not over the view bar of a view on the right (its offset text)."""
        width, height = INSPECTOR_HANDLE_SIZE
        parentHeight = self.parentWidget.height
        top = max(0, (parentHeight - height) // 2)
        for bandTop, bandBottom in sorted(self._viewBarBands()):
            if top < bandBottom and top + height > bandTop:
                below = bandBottom + 4
                top = below if below + height <= parentHeight else max(0, bandTop - height - 4)
        self.button.move(self.parentWidget.width - width, top)

    def raise_(self):
        if self.button.isVisible():
            self.button.raise_()

    def dispose(self):
        ui.removePaletteListener(self.refreshIcon)
        ui.bindStyleSheet(self.button, None)
        self.button.hide()
        self.button.setObjectName("")
        self.button.deleteLater()


#
# Event filter
#


class _ShellEventFilter(qt.QObject):
    def __init__(self, shell):
        qt.QObject.__init__(self)
        self.shell = shell

    def eventFilter(self, obj, event):
        try:
            # Only the Esc key of the Esc stack is consumed (True)
            return bool(self.shell._onEvent(obj, event))
        except Exception:
            logging.error(f"Studio event handler failed:\n{traceback.format_exc()}")
        return False


#
# Shell
#


class Shell:
    """The Studio shell of the main window. Use ``instance()`` to get the live shell."""

    def __init__(self, force=False):
        """Create the shell. ``force=True`` allows applying it under ``--testing`` (Studio tests)."""
        global _shell
        self.mainWindow = slicer.util.mainWindow()
        if self.mainWindow is None:
            raise RuntimeError("The Studio shell needs the application main window")
        if slicer.app.testingEnabled() and not force:
            raise RuntimeError("The Studio shell does not run when testing is enabled (use force=True in tests)")
        self.force = force
        #: Duration of the last apply() in milliseconds (including the import of the shell when applied at startup)
        self.applyDurationMs = None
        #: Study session of the Library (set by the study plug-in), None if no study is open
        self.studySession = None
        self.hoverTracker = None
        #: Scroll area of the module panel (PageUp and PageDown over a slice view go to the view)
        self._panelScrollArea = None
        self._forwardingSliceKey = False
        self.workspaces = None
        self.topBar = None
        self.panelHeader = None
        self._active = False
        self._classicState = None
        self._classicSwitchAction = None
        self._classicSwitchSeparator = None
        self._canvas = None
        self._renderPausedByCanvas = False
        #: (view area widget, its maximum size) while the canvas holds the size of the view area
        self._heldViewArea = None
        self._toast = None
        self._footerSlot = None
        self._footerWidget = None
        self._primaryAction = None
        self._primaryActionLabel = ""
        self._shortcuts = []
        self._workspaceShortcuts = {}
        self._installedWorkspaceShortcuts = []
        self._addedActions = []
        self._watchedToolBars = []
        #: [toolBar, visible in Classic] of the toolbars that the classic window state does not restore
        #: (added after Studio was applied, or without objectName)
        self._unsavedToolBars = []
        self._classicToolBarNames = set()
        self._toolBarScanTimer = None
        self._plugins = []
        self._connections = []
        #: The Studio look of the settings dialog (``SettingsDialogLook``), while Studio is applied
        self._settingsLook = None
        self._focusModeHidden = None
        self._focusModePill = None
        self._panelWidthTimer = None
        self._panelUpdateTimer = None
        self._pendingPanelShrink = False
        self._windowOnScreenTimer = None
        #: True while the main window is larger than its screen because Qt grew it to its minimum size (a layout or a
        #: module needed more room), not because the user sized or placed it so: see keepWindowOnScreen()
        self._windowGrownForMinimum = False
        #: Size and position keepWindowOnScreen() last gave the main window (another size or position is the user's)
        self._windowResizedTo = None
        self._windowMovedTo = None
        #: Widest inspector width needed by each workspace module ({module name: px}), see _workspacePanelWidth()
        self._workspacePanelWidths = {}
        #: Contents of the module panel scroll area, watched for layout changes (a tool was chosen)
        self._watchedPanelContents = None
        #: True while the inspector is hidden by the user (shortcut, Ctrl+5, menu), not by a workspace page
        self._inspectorHiddenByUser = False
        #: (toggle view action, slot) of the toolbars that Studio hides
        self._toolBarToggleConnections = []
        #: Toolbars without objectName that the user showed in this session (not persisted)
        self._sessionAllowedToolBars = []
        #: Classic minimum width of the module panel and policy of its horizontal scroll bar
        self._classicModulePanelState = None
        #: ctkFittedTextBrowser widgets given an explicit minimum width in Studio
        self._fittedTextBrowsers = []
        #: True from an Esc shortcut override that the Esc stack handled to the matching key press
        self._escapeHandled = False
        self._eventFilter = None
        #: Studio overlays that Esc closes (rung 0.5): [(widget, close function)], the top-most last
        self._escapeOverlays = []
        #: Links of the plug-ins at the end of the help popover: [(key, text, callback, isEnabled or None)]
        self._helpLinks = []
        self._stickyToast = None
        self._inspectorHandle = None
        #: Inspector notice slot (above the module panel) and its notice: [widget, key, workspace key]
        self._noticeSlot = None
        self._notice = None
        #: Last toast messages: (time.time(), text, level)
        self._recentMessages = collections.deque(maxlen=RECENT_MESSAGES_COUNT)
        #: Workspace that the current chain of transient modules was opened from (label of the back button)
        self._transientOrigin = None
        self._classicFileAddDataShortcuts = []
        #: Shortcuts of the main window actions that Studio retargets ({objectName: [QKeySequence]})
        self._classicActionShortcuts = {}
        #: (text, tooltip) of the main window actions that Studio renames ({objectName: (text, tooltip)})
        self._classicActionTexts = {}
        self._classicStatusBarActionEnabled = None
        #: Menu items given a mnemonic in Studio: [(action, classic text, Studio text)]
        self._mnemonicActions = []
        #: Scene root directory before Studio replaced it (the Ctrl+S folder), and the one Studio set
        self._classicSceneRoot = None
        self._studioSceneRoot = None
        self._sceneCloseObservation = None
        self._sceneImportObservation = None
        #: Focus policies of the view widgets covered by the canvas ({id(widget): (widget, policy)})
        self._coveredFocusPolicies = {}
        #: Widgets of the view area whose focus policy accepts Tab, visible or not, found by the last full scan
        #: (None: scan again, after a layout change or a Classic period)
        self._viewTabStops = None
        #: A bare Alt press waits for its release (it opens the ≡ menu on Windows and Linux)
        self._altPending = False
        self._altSeenHeld = False
        self._altTimer = None
        if _shell is None:
            _shell = self

    #
    # State
    #

    def isActive(self):
        """True while the Studio interface is shown."""
        return self._active

    def currentWorkspace(self):
        """Key of the current workspace ("library", "draw" or "map"), None for a transient module or in Classic."""
        if not self._active or self.workspaces is None:
            return None
        return self.workspaces.currentWorkspace()

    def currentModuleName(self):
        """Name of the module shown in the inspector."""
        return self.mainWindow.moduleSelector().selectedModule

    def _modulePanel(self):
        return self._cachedChild("QWidget", "ModulePanel")

    def _panelDock(self):
        return self._cachedChild("QDockWidget", "PanelDockWidget")

    def _cachedChild(self, className, objectName):
        """A child of the main window that lives as long as it (found once: findChild() walks thousands of widgets)."""
        cache = self.__dict__.setdefault("_childCache", {})
        widget = cache.get(objectName)
        if widget is not None:
            try:
                if widget.objectName == objectName:
                    return widget
            except Exception:
                # Deleted
                pass
        widget = self.mainWindow.findChild(className, objectName)
        cache[objectName] = widget
        return widget

    def _dataProbe(self):
        return self.mainWindow.findChild("QWidget", "DataProbeCollapsibleWidget")

    def _fileAddDataAction(self):
        return self.mainWindow.findChild("QAction", "FileAddDataAction")

    def _connect(self, sender, signal, slot):
        sender.connect(signal, slot)
        self._connections.append((sender, signal, slot))

    def _disconnectAll(self):
        for sender, signal, slot in reversed(self._connections):
            try:
                sender.disconnect(signal, slot)
            except Exception:
                pass
        self._connections = []

    #
    # Classic switch (available in both interfaces)
    #

    def installClassicSwitch(self):
        """Add the checkable "Studio interface" action (Ctrl+Alt+0) to the View menu and the main window."""
        if self._classicSwitchAction is not None:
            return
        action = qt.QAction(self._classicSwitchText(), self.mainWindow)
        action.setObjectName("StudioInterfaceAction")
        action.setCheckable(True)
        action.setChecked(self._active)
        action.setShortcut(qt.QKeySequence("Ctrl+Alt+0"))
        action.setToolTip(_("Switch between the Studio and the Classic interface (Ctrl+Alt+0)"))
        action.setProperty("studioDescription", _("Classic ⇄ Studio interface"))
        action.connect("triggered(bool)", self._onClassicSwitchTriggered)
        viewMenu = self.mainWindow.findChild("QMenu", "ViewMenu")
        if viewMenu is not None:
            self._classicSwitchSeparator = viewMenu.addSeparator()
            viewMenu.addAction(action)
        # Keep the shortcut working when the menubar is hidden
        self.mainWindow.addAction(action)
        self._classicSwitchAction = action
        installCloseGuard(self.mainWindow).classicStateProvider = self._classicStateIfActive

    def _onClassicSwitchTriggered(self, checked):
        if checked:
            self.studio()
        else:
            self.classic()

    def _classicSwitchText(self):
        # In Classic the item says what it does; in Studio it is the checked state of the interface
        return _("Studio interface") if self._active else _("Switch to Studio interface")

    def _updateClassicSwitchAction(self):
        if self._classicSwitchAction is not None:
            self._classicSwitchAction.setChecked(self._active)
            self._classicSwitchAction.setText(self._classicSwitchText())

    def _classicStateIfActive(self):
        return self._classicState if self._active else None

    def toggleClassic(self):
        if self._active:
            self.classic()
        else:
            self.studio()

    def studio(self):
        """Switch to the Studio interface and remember the choice.

        Coming from Classic in the middle of a session, Studio returns to the workspace of the current
        module (Draw for the Segment Editor), else to the last workspace (``Studio/LastWorkspace``).
        """
        qt.QSettings().setValue("Studio/Mode", "studio")
        self.apply(midSession=True)

    def classic(self, persist=True):
        """Restore the Classic interface exactly as it was (SPEC 2.7) and remember the choice."""
        if not self._active:
            return
        self._restoreClassic()
        if persist:
            qt.QSettings().setValue("Studio/Mode", "classic")
        self._updateClassicSwitchAction()

    def _restoreClassic(self):
        """Undo apply(). Also used to recover from a failed apply(), so each part may be missing."""
        mainWindow = self.mainWindow
        self._applyNavigationBehaviours(False)
        if self._settingsLook is not None:
            try:
                self._settingsLook.restore()
            except Exception:
                logging.error(f"Studio: restoring the settings dialog failed:\n{traceback.format_exc()}")
            self._settingsLook = None
        if self.workspaces is not None:
            self.workspaces.leaveCurrent()
        self.setCanvas(None)
        self.setFocusMode(False)
        self._restoreCoveredFocusPolicies()
        # The layout may change in Classic, where _onLayoutChanged is not connected
        self._viewTabStops = None
        self._uninstallPlugins()
        self._setSegmentEditorPresentation("classic")
        self._setMapPresentation(False)
        self._active = False
        self._pauseLesionQc()
        self._disconnectAll()
        self._removeEventFilters()
        self._escapeHandled = False
        self._inspectorHiddenByUser = False
        self._escapeOverlays = []
        self._transientOrigin = None
        self._cancelAltKey()
        if self._sceneCloseObservation is not None:
            slicer.mrmlScene.RemoveObserver(self._sceneCloseObservation)
            self._sceneCloseObservation = None
        if self._sceneImportObservation is not None:
            slicer.mrmlScene.RemoveObserver(self._sceneImportObservation)
            self._sceneImportObservation = None
        self._restoreSceneRootDirectory()
        for shortcut in self._installedWorkspaceShortcuts + self._shortcuts:
            self._deleteShortcut(shortcut)
        self._installedWorkspaceShortcuts = []
        self._shortcuts = []
        for timer in (self._toolBarScanTimer, self._panelUpdateTimer, self._panelWidthTimer, self._windowOnScreenTimer):
            if timer is not None:
                timer.stop()
        self._toolBarScanTimer = None
        self._panelUpdateTimer = None
        if self._windowOnScreenTimer is not None:
            self._windowOnScreenTimer.disconnect("timeout()", self.keepWindowOnScreen)
            self._windowOnScreenTimer = None
        self._windowGrownForMinimum = False
        self._windowResizedTo = None
        self._windowMovedTo = None
        self._pendingPanelShrink = False
        for toolBar, slot in self._watchedToolBars:
            try:
                toolBar.disconnect("visibilityChanged(bool)", slot)
            except Exception:
                pass
        self._watchedToolBars = []
        for action, slot in self._toolBarToggleConnections:
            try:
                action.disconnect("triggered(bool)", slot)
            except Exception:
                pass
        self._toolBarToggleConnections = []
        self._sessionAllowedToolBars = []
        for action in self._addedActions:
            mainWindow.removeAction(action)
        self._addedActions = []
        fileAddDataAction = self._fileAddDataAction()
        if fileAddDataAction is not None:
            fileAddDataAction.setShortcuts(self._classicFileAddDataShortcuts)
        for objectName, shortcuts in self._classicActionShortcuts.items():
            action = mainWindow.findChild("QAction", objectName)
            if action is not None:
                action.setShortcuts(shortcuts)
        self._classicActionShortcuts = {}
        for objectName, (text, toolTip) in self._classicActionTexts.items():
            action = mainWindow.findChild("QAction", objectName)
            if action is not None:
                action.setText(text)
                action.setToolTip(toolTip)
        self._classicActionTexts = {}
        statusBarAction = mainWindow.findChild("QAction", "ShowStatusBarAction")
        if statusBarAction is not None and self._classicStatusBarActionEnabled is not None:
            statusBarAction.setEnabled(self._classicStatusBarActionEnabled)
        self._classicStatusBarActionEnabled = None
        self._removeMenuMnemonics()

        if self.topBar is not None:
            self.topBar.dispose()
            self.topBar = None
        if self._toast is not None:
            self._toast.dispose()
            self._toast = None
        if self._stickyToast is not None:
            self._stickyToast.dispose()
            self._stickyToast = None
        if self._inspectorHandle is not None:
            self._inspectorHandle.dispose()
            self._inspectorHandle = None
        if self._focusModePill is not None:
            self._focusModePill.dispose()
            self._focusModePill = None
        if self.hoverTracker is not None:
            self.hoverTracker.dispose()
            self.hoverTracker = None
        if self.workspaces is not None:
            self.workspaces.dispose()
            self.workspaces = None

        dock = self._panelDock()
        self._removeFooterSlot()
        self._removeNoticeSlot()
        if dock.titleBarWidget() != self._classicTitleBar:
            dock.setTitleBarWidget(self._classicTitleBar)
            if self._classicTitleBar is not None:
                self._classicTitleBar.show()
        if self.panelHeader is not None:
            self.panelHeader.dispose()
            self.panelHeader = None
        dock.setProperty("features", self._classicDockFeatures)
        self._setModulePanelConstrained(False)

        mainWindow.restoreState(self._classicState)
        for toolBar, visible in self._unsavedToolBars:
            try:
                # Skip toolbars that their owner removed from the main window (removeToolBar() hides them in
                # Classic) or reparented: showing them would draw a stray widget over the main window
                if (visible and toolBar.isHidden() and self._isMainWindowToolBar(toolBar)
                        and toolBar.objectName not in self._classicToolBarNames):
                    toolBar.show()
            except Exception:
                # Deleted by its owner
                pass
        self._unsavedToolBars = []
        mainWindow.menuBar().setVisible(self._classicVisibility["menuBar"])
        if mainWindow.statusBar() is not None:
            mainWindow.statusBar().setVisible(self._classicVisibility["statusBar"])
        dataProbe = self._dataProbe()
        if dataProbe is not None:
            dataProbe.setVisible(self._classicVisibility["dataProbe"])
        self._modulePanel().helpAndAcknowledgmentVisible = self._classicVisibility["help"]
        sequences = getattr(slicer.modules, "sequences", None)
        if sequences is not None and self._classicVisibility.get("sequencesAutoShowToolBar") is not None:
            sequences.autoShowToolBar = self._classicVisibility["sequencesAutoShowToolBar"]

    #
    # Apply
    #

    def apply(self, startTime=None, midSession=False):
        """Show the Studio interface (SPEC 2.3).

        At startup Studio shows ``Studio/StartWorkspace``. With ``midSession`` (from Classic, see
        ``studio()``) it shows the workspace of the current module, else the last workspace.
        """
        if self._active:
            return
        if startTime is None:
            startTime = time.perf_counter()
        try:
            self._apply(midSession=midSession)
        except Exception:
            logging.error(f"Studio could not be applied, restoring the Classic interface:\n{traceback.format_exc()}")
            if self._active:
                try:
                    self._restoreClassic()
                except Exception:
                    logging.error(f"Studio: restoring the Classic interface failed:\n{traceback.format_exc()}")
            self._updateClassicSwitchAction()
            raise
        self._updateClassicSwitchAction()
        self.applyDurationMs = (time.perf_counter() - startTime) * 1000.0
        logging.debug(f"Studio applied in {self.applyDurationMs:.1f} ms")

    def _apply(self, midSession=False):
        mainWindow = self.mainWindow
        settings = qt.QSettings()
        dock = self._panelDock()
        modulePanel = self._modulePanel()
        dataProbe = self._dataProbe()

        # 1. Remember the classic state
        if self._dockAreasFollowSizeHints():
            # Applied at startup, before the first layout of the window: saveState() would store the
            # pre-layout width of the module panel. Store the width that the first layout gives it.
            width = min(max(dock.sizeHint.width(), dock.minimumSizeHint.width(), dock.minimumWidth), dock.maximumWidth)
            mainWindow.resizeDocks([dock], [width], qt.Qt.Horizontal)
        self._classicState = mainWindow.saveState()
        settings.setValue("Studio/ClassicWindowState", self._classicState)
        self._classicToolBarNames = {toolBar.objectName for toolBar in self._mainWindowToolBars() if toolBar.objectName}
        self._unsavedToolBars = []
        self._classicTitleBar = dock.titleBarWidget()
        # Through the property() / setProperty() pair: with Qt 6, dock.features is an opaque object that setFeatures() rejects
        self._classicDockFeatures = dock.property("features")
        sequences = getattr(slicer.modules, "sequences", None)
        self._classicVisibility = {
            "menuBar": not mainWindow.menuBar().isHidden(),
            "statusBar": mainWindow.statusBar() is not None and not mainWindow.statusBar().isHidden(),
            "dataProbe": dataProbe is not None and not dataProbe.isHidden(),
            "help": modulePanel.helpAndAcknowledgmentVisible,
            "sequencesAutoShowToolBar": sequences.autoShowToolBar if sequences is not None else None,
        }
        fileAddDataAction = self._fileAddDataAction()
        self._classicFileAddDataShortcuts = list(fileAddDataAction.shortcuts()) if fileAddDataAction is not None else []
        # Ctrl+F (module finder) and Ctrl+H (home module) are retargeted; Ctrl+H is left alone on macOS (⌘H hides the application)
        retargeted = ["ViewFindModuleAction"] if isMacOS() else ["ViewFindModuleAction", "ModuleHomeAction"]
        self._classicActionShortcuts = {}
        for objectName in retargeted:
            action = mainWindow.findChild("QAction", objectName)
            if action is not None:
                self._classicActionShortcuts[objectName] = list(action.shortcuts())
        maximizeViewAction = mainWindow.findChild("QAction", "WindowMaximizeViewAction")
        self._classicActionTexts = {"WindowMaximizeViewAction": (maximizeViewAction.text, maximizeViewAction.toolTip)} if maximizeViewAction is not None else {}
        statusBarAction = mainWindow.findChild("QAction", "ShowStatusBarAction")
        self._classicStatusBarActionEnabled = statusBarAction.enabled if statusBarAction is not None else None

        # 2. Hide the classic chrome (nothing is deleted)
        self._active = True
        self._eventFilter = _ShellEventFilter(self)
        self.workspaces = workspaces.WorkspaceController(self)
        mainWindow.menuBar().hide()
        if mainWindow.statusBar() is not None:
            mainWindow.statusBar().hide()
        for toolBar in self._mainWindowToolBars():
            self._hideToolBar(toolBar)
        # Toolbars added later (extensions) are hidden too: see _onEvent()
        self._toolBarScanTimer = qt.QTimer()
        self._toolBarScanTimer.setSingleShot(True)
        self._toolBarScanTimer.setInterval(0)
        self._toolBarScanTimer.connect("timeout()", self._hideNewToolBars)
        if sequences is not None:
            sequences.autoShowToolBar = False
        if dataProbe is not None:
            dataProbe.hide()
        modulePanel.helpAndAcknowledgmentVisible = False

        # 3. Keep the shortcuts of the hidden menus and toolbars working
        self._attachChromeShortcutActions()
        if fileAddDataAction is not None:
            # A second Ctrl+O shortcut would be ambiguous: Qt would trigger neither
            fileAddDataAction.setShortcuts([])
        self._addShortcut("Ctrl+O", self.openFiles, _("Open files"))
        self._addShortcut("Ctrl+Return", self.triggerPrimaryAction, _("Primary action of the inspector"))
        self._addShortcut("Ctrl+Enter", self.triggerPrimaryAction, _("Primary action of the inspector"))
        # The classic Ctrl+F and Ctrl+H actions lose their keys (two shortcuts on one key would be ambiguous)
        for objectName in self._classicActionShortcuts:
            mainWindow.findChild("QAction", objectName).setShortcuts([])
        self._addShortcut("Ctrl+F", lambda: self.openPalette("modules"), _("Search modules"))
        if "ModuleHomeAction" in self._classicActionShortcuts:
            self._addShortcut("Ctrl+H", lambda: self.switchTo("library"), _("Library workspace"))
        if not hasattr(mainWindow, "setFocusMode"):
            for keySequence in FOCUS_MODE_SHORTCUTS:
                self._addShortcut(keySequence, self.toggleFocusMode, _("Hide panels"))
        # Not Tab: Tab and Shift+Tab move the keyboard focus (out of the views too). The second key of the
        # inspector, Ctrl+5, is the shortcut of the dock widget's toggle view action (View > Module Panel).
        self._addShortcut(INSPECTOR_SHORTCUT, self.toggleInspector, _("Show or hide the inspector"))
        self._mnemonicActions = []
        self._addMenuMnemonics()
        for menuName in {menuName for menuName, moduleName, word in self._STUDIO_MNEMONICS}:
            menu = mainWindow.findChild("QMenu", menuName)
            if menu is not None:
                self._connect(menu, "aboutToShow()", self._addMenuMnemonics)
        # Help > Keyboard Shortcuts Reference and View: Ctrl+6 is "Hide panels" in Studio (restored in Classic)
        if maximizeViewAction is not None:
            maximizeViewAction.setText(_("Hide panels"))
            maximizeViewAction.setToolTip(ui.tooltip(_("Hide panels"), focusModeKeyText(), _("Hide the inspector and the top bar")))

        # 4. Inspector on the right with the Studio panel header and a footer slot
        if dock.floating:
            dock.setFloating(False)
        # Not movable or floatable. Closable keeps its toggle view action (Ctrl+5, View > Module Panel)
        # enabled; the panel header has no title bar close button.
        dock.setFeatures(qt.QDockWidget.DockWidgetClosable)
        mainWindow.addDockWidget(qt.Qt.RightDockWidgetArea, dock)
        dock.show()
        self.panelHeader = PanelHeader(self)
        dock.setTitleBarWidget(self.panelHeader.widget)
        self._insertFooterSlot()
        self._insertNoticeSlot()
        self._setModulePanelConstrained(True)
        # The widths that the workspace modules needed in earlier sessions: the first workspace has the width
        # of all workspaces at once
        for ws in workspaces.WORKSPACES:
            self._workspacePanelWidth(ws.moduleName, 0)
        mainWindow.resizeDocks([dock], [max([self.panelWidth, *self._workspacePanelWidths.values()])], qt.Qt.Horizontal)

        # 5. Top bar
        self.hoverTracker = layouts.HoveredSliceViewTracker()
        self.topBar = topbar.TopBar(self)
        mainWindow.addToolBar(qt.Qt.TopToolBarArea, self.topBar.toolBar)
        self.topBar.toolBar.show()
        self._updateTopBarCompact()
        self._makeTopBarFocusable()
        self._addTopBarShortcuts()
        self._toast = hud.Toast(mainWindow.centralWidget())
        self._stickyToast = StickyToast(mainWindow.centralWidget())
        self._focusModePill = hud.CornerPill(mainWindow.centralWidget(), "StudioExitFocusModeButton", _("Show panels"), "minimize-2",
                                             lambda: self.setFocusMode(False), ui.tooltip(_("Show panels"), focusModeKeyText(), _("or Esc")))
        self._inspectorHandle = InspectorHandle(self, mainWindow.centralWidget())

        # 6. Layouts (registered in C++ by the fork, only added here on builds that lack them)
        layouts.register()

        self._connect(mainWindow.moduleSelector(), "moduleSelected(QString)", self.workspaces.onModuleSelected)
        self._connect(modulePanel, "moduleAdded(QString)", self._onModuleAdded)
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            self.workspaces.resetLayoutChoice()
            self._connect(layoutManager, "layoutChanged(int)", self.workspaces.onLayoutChanged)
            self._connect(layoutManager, "layoutChanged(int)", self._onLayoutChanged)
        self._connect(dock.toggleViewAction(), "triggered(bool)", self._onPanelToggleTriggered)
        errorLogDock = mainWindow.errorLogDockWidget() if hasattr(mainWindow, "errorLogDockWidget") else None
        if errorLogDock is not None:
            self._connect(errorLogDock, "visibilityChanged(bool)", self._onErrorLogVisibilityChanged)
        viewMenu = mainWindow.findChild("QMenu", "ViewMenu")
        if viewMenu is not None:
            self._connect(viewMenu, "aboutToShow()", self._onViewMenuAboutToShow)
        settingsDialog = slicer.app.settingsDialog()
        if settingsDialog is not None:
            # The navigation settings (Studio/*) may have changed
            self._connect(settingsDialog, "finished(int)", self._onSettingsDialogFinished)
            # The Studio look of the dialog (V-02), undone in Classic
            if self._settingsLook is None:
                self._settingsLook = SettingsDialogLook(settingsDialog)
        mainWindow.installEventFilter(self._eventFilter)
        mainWindow.centralWidget().installEventFilter(self._eventFilter)
        dock.installEventFilter(self._eventFilter)
        # PageUp and PageDown that the inspector would use to scroll itself step the slice view under the pointer
        self._panelScrollArea = dock.findChild("QScrollArea", "ScrollArea")
        if self._panelScrollArea is not None:
            self._panelScrollArea.installEventFilter(self._eventFilter)

        # 7. Draw presentation of the Segment Editor and the Studio look of Map, if they exist already. The folder of the
        # Save dialog (Ctrl+S).
        self._setSegmentEditorPresentation("studio")
        self._setMapPresentation(True)
        self._fixSceneRootDirectory()
        self._sceneCloseObservation = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.EndCloseEvent, self._onSceneEndClose)
        self._sceneImportObservation = slicer.mrmlScene.AddObserver(slicer.vtkMRMLScene.EndImportEvent, self._onSceneEndImport)
        self._installPlugins()

        # Build the heavy workspace pages when the application is idle
        qt.QTimer.singleShot(300, lambda: self._prewarm("SegmentEditor"))
        qt.QTimer.singleShot(600, lambda: self._prewarm("LesionNetworkMapping"))

        # 8. Start workspace
        self.switchTo(self._midSessionWorkspace() if midSession else self._startWorkspace())

        # 9. Navigation behaviours of the views that only Studio uses
        self._applyNavigationBehaviours(True)

    @staticmethod
    def _availableWorkspaceKey(keyOrTitle):
        ws = workspaces.workspace(keyOrTitle)
        return ws.key if ws is not None and workspaces.isAvailable(ws) else None

    def _startWorkspace(self):
        """Workspace shown when Studio is applied at startup (``Studio/StartWorkspace``, "Last" allowed)."""
        startWorkspace = slicer.util.settingsValue("Studio/StartWorkspace", "Library")
        if str(startWorkspace).lower() == "last":
            startWorkspace = slicer.util.settingsValue("Studio/LastWorkspace", "Library")
        return self._availableWorkspaceKey(startWorkspace) or "library"

    def _midSessionWorkspace(self):
        """Workspace shown when the user comes back from Classic: the one of the current module, else the last one."""
        ws = workspaces.workspaceForModule(self.currentModuleName())
        key = self._availableWorkspaceKey(ws.key) if ws is not None else None
        return key or self._availableWorkspaceKey(slicer.util.settingsValue("Studio/LastWorkspace", "Library")) or "library"

    def _dockAreasFollowSizeHints(self):
        """True while the main window is shown for the first time (not laid out yet) and Slicer did
        not restore a saved window state: the first layout then sizes the module panel from its size
        hint, but saveState() still holds its pre-layout width.
        """
        mainWindow = self.mainWindow
        dock = self._panelDock()
        if mainWindow.testAttribute(qt.Qt.WA_Mapped) or dock.floating or dock.isHidden():
            return False
        settings = qt.QSettings()
        if not slicer.util.toBool(settings.value("MainWindow/RestoreGeometry", False)):
            return True
        savedState = settings.value("MainWindow/windowState")
        return savedState.isEmpty() if hasattr(savedState, "isEmpty") else not savedState

    def _isMainWindowToolBar(self, toolBar):
        """True if the toolbar is in a toolbar area of the main window (floating or not)."""
        mainWindow = self.mainWindow
        return toolBar.parentWidget() == mainWindow and mainWindow.toolBarArea(toolBar) != qt.Qt.NoToolBarArea

    def _mainWindowToolBars(self):
        """Toolbars of the main window (in its toolbar areas, floating or not)."""
        return [toolBar for toolBar in self.mainWindow.findChildren("QToolBar") if self._isMainWindowToolBar(toolBar)]

    def _hideNewToolBars(self):
        """Hide the toolbars added to the main window since Studio was applied (extensions)."""
        if not self._active:
            return
        watched = [toolBar for toolBar, slot in self._watchedToolBars]
        for toolBar in self._mainWindowToolBars():
            if toolBar not in watched:
                self._hideToolBar(toolBar)

    def _hideToolBar(self, toolBar):
        if self.topBar is not None and toolBar == self.topBar.toolBar:
            return
        if toolBar.objectName == "StudioTopBar":
            return
        if toolBar not in [watched for watched, slot in self._watchedToolBars]:
            slot = lambda visible, toolBar=toolBar: self._onToolBarVisibilityChanged(toolBar, visible)
            toolBar.connect("visibilityChanged(bool)", slot)
            self._watchedToolBars.append((toolBar, slot))
            # View > Toolbars: triggered() is only emitted for the user (or trigger()), not when the toolbar
            # shows itself or is shown by code
            toggleViewAction = toolBar.toggleViewAction()
            toggleSlot = lambda checked, toolBar=toolBar: self._onToolBarToggledByUser(toolBar, checked)
            toggleViewAction.connect("triggered(bool)", toggleSlot)
            self._toolBarToggleConnections.append((toggleViewAction, toggleSlot))
            if toolBar.objectName not in self._classicToolBarNames:
                # restoreState() does not restore it: remember if it is shown, or about to be shown by
                # the main window layout (hidden but not explicitly)
                visible = not (toolBar.isHidden() and toolBar.testAttribute(qt.Qt.WA_WState_ExplicitShowHide))
                self._unsavedToolBars.append([toolBar, visible])
        if not self._isToolBarAllowed(toolBar):
            toolBar.hide()

    @staticmethod
    def _allowedToolBars():
        """Object names of the toolbars shown in Studio (``Studio/AllowedToolbars``)."""
        allowed = slicer.util.settingsValue("Studio/AllowedToolbars", [])
        if isinstance(allowed, str):
            allowed = [allowed] if allowed else []
        return [str(name) for name in (allowed or [])]

    def _isToolBarAllowed(self, toolBar):
        if toolBar.objectName:
            return toolBar.objectName in self._allowedToolBars()
        return toolBar in self._sessionAllowedToolBars

    def _onToolBarVisibilityChanged(self, toolBar, visible):
        if not visible or not self._active:
            return
        for entry in self._unsavedToolBars:
            if entry[0] == toolBar:
                # Its extension showed it: show it in Classic too
                entry[1] = True
        if self._isToolBarAllowed(toolBar):
            return
        # An extension toolbar showed itself (for example the sequence browser): hide it again, unless the
        # user showed it from View > Toolbars (handled by _onToolBarToggledByUser() before the timer fires)
        qt.QTimer.singleShot(0, lambda: toolBar.hide() if self._active and not self._isToolBarAllowed(toolBar) else None)

    def _onToolBarToggledByUser(self, toolBar, checked):
        """The user showed or hid a toolbar from View > Toolbars: remember it in ``Studio/AllowedToolbars``."""
        if not self._active:
            return
        name = toolBar.objectName
        if name:
            allowed = self._allowedToolBars()
            if checked and name not in allowed:
                allowed.append(name)
            elif not checked and name in allowed:
                allowed.remove(name)
            else:
                return
            settings = qt.QSettings()
            if allowed:
                settings.setValue("Studio/AllowedToolbars", allowed)
            else:
                settings.remove("Studio/AllowedToolbars")
        elif checked and toolBar not in self._sessionAllowedToolBars:
            self._sessionAllowedToolBars.append(toolBar)
        elif not checked and toolBar in self._sessionAllowedToolBars:
            self._sessionAllowedToolBars.remove(toolBar)

    def _chromeShortcutActions(self):
        """Actions with a shortcut that only belong to the menubar, the status bar or hidden toolbars."""
        mainWindow = self.mainWindow
        roots = [mainWindow.menuBar()]
        if mainWindow.statusBar() is not None:
            roots.append(mainWindow.statusBar())
        roots += [toolBar for toolBar, slot in self._watchedToolBars]

        def isChrome(widget, depth=0):
            if widget is None or depth > 8:
                return False
            if widget.inherits("QMenu"):
                owners = associatedWidgets(widget.menuAction())
                if owners:
                    return all(isChrome(owner, depth + 1) for owner in owners)
            ancestor = widget
            while ancestor is not None and ancestor != mainWindow:
                if ancestor in roots:
                    return True
                ancestor = ancestor.parentWidget()
            return False

        actions = []
        for action in mainWindow.findChildren("QAction"):
            if action.objectName in _KEEP_UNATTACHED_ACTIONS or not action.shortcuts():
                continue
            if action.shortcutContext not in (qt.Qt.WindowShortcut, qt.Qt.ApplicationShortcut):
                continue
            owners = associatedWidgets(action)
            if not owners or mainWindow in owners:
                continue
            if all(isChrome(owner) for owner in owners):
                actions.append(action)
        return actions

    def _attachChromeShortcutActions(self):
        self._addedActions = []
        for action in self._chromeShortcutActions():
            self.mainWindow.addAction(action)
            self._addedActions.append(action)

    def _insertFooterSlot(self):
        contents = self._panelDock().widget()
        layout = contents.layout()
        self._footerSlot = qt.QWidget()
        self._footerSlot.setObjectName("StudioInspectorFooter")
        footerLayout = qt.QVBoxLayout(self._footerSlot)
        footerLayout.setContentsMargins(0, 0, 0, 0)
        footerLayout.setSpacing(0)
        self._footerSlot.hide()
        layout.insertWidget(layout.indexOf(self._modulePanel()) + 1, self._footerSlot)

    def _removeFooterSlot(self):
        if self._footerSlot is None:
            return
        self.setInspectorFooter(None)
        layout = self._panelDock().widget().layout()
        layout.removeWidget(self._footerSlot)
        self._footerSlot.hide()
        self._footerSlot.setObjectName("")
        self._footerSlot.deleteLater()
        self._footerSlot = None

    def _insertNoticeSlot(self):
        """The inspector notice slot, directly above the module panel (USABILITY 6.4)."""
        contents = self._panelDock().widget()
        layout = contents.layout()
        self._noticeSlot = qt.QWidget()
        self._noticeSlot.setObjectName("StudioInspectorNotice")
        # Ignored: a notice never sets the minimum width of the inspector (notices are height-for-width)
        policy = qt.QSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Maximum)
        policy.setHeightForWidth(True)
        self._noticeSlot.setSizePolicy(policy)
        noticeLayout = qt.QVBoxLayout(self._noticeSlot)
        # A rounded notice (VISUAL 4.8) inset like the inspector cards
        noticeLayout.setContentsMargins(12, 8, 12, 4)
        noticeLayout.setSpacing(0)
        self._noticeSlot.hide()
        layout.insertWidget(layout.indexOf(self._modulePanel()), self._noticeSlot)

    def _removeNoticeSlot(self):
        if self._noticeSlot is None:
            return
        self.setInspectorNotice(None)
        layout = self._panelDock().widget().layout()
        layout.removeWidget(self._noticeSlot)
        self._noticeSlot.hide()
        self._noticeSlot.setObjectName("")
        self._noticeSlot.deleteLater()
        self._noticeSlot = None

    def _makeTopBarFocusable(self):
        """The buttons of the top bar take the keyboard focus with Tab (not by a click), and Space and Enter press them."""
        topBar = self.topBar
        buttons = [getattr(topBar, name, None) for name in ("menuButton", "breadcrumbButton", "undoButton", "redoButton", "layoutButton", "errorBadge")]
        buttons += list(getattr(topBar, "workspaceButtons", {}).values())
        for button in buttons:
            if button is None:
                continue
            if button.focusPolicy == qt.Qt.NoFocus:
                button.setFocusPolicy(qt.Qt.TabFocus)
            ui.installButtonKeys(button)

    def _addTopBarShortcuts(self):
        for index, ws in enumerate(workspaces.WORKSPACES):
            self._addShortcut(f"Ctrl+Alt+{index + 1}", lambda key=ws.key: self.switchTo(key), _("{workspace} workspace").format(workspace=ws.title))
        self._addShortcut("Ctrl+K", self.openPalette, _("Search"))
        self._addShortcut("Ctrl+/", self.toggleCheatSheet, _("Keyboard shortcuts"))
        for key, title, shortcut, iconName, layoutId in layouts.PICKER_ENTRIES:
            self._addShortcut(shortcut, lambda key=key: self.setLayoutFromPicker(key), _("{layout} layout").format(layout=title))
        # Keyboard access to the menus and to the regions of the window (USABILITY 9.1)
        self._addShortcut(APP_MENU_SHORTCUT, self.showAppMenu, _("Menu"))
        if not isMacOS():
            # On macOS the native menu bar stays reachable (Ctrl+F2) and Option+letter types characters
            for keySequence, menuName in APP_SUBMENU_SHORTCUTS:
                self._addShortcut(keySequence, lambda menuName=menuName: self.showAppMenu(menuName), _("Menu: {menu}").format(menu=menuName[:-4]))
        self._addShortcut(FOCUS_REGION_SHORTCUTS[0], lambda: self.cycleFocusRegion(1), _("Next region (top bar, inspector, views)"))
        self._addShortcut(FOCUS_REGION_SHORTCUTS[1], lambda: self.cycleFocusRegion(-1), _("Previous region"))

    #
    # Shortcuts
    #

    def _createShortcut(self, keySequence, callback, description, parent=None, context=qt.Qt.WindowShortcut):
        shortcut = qt.QShortcut(qt.QKeySequence(keySequence), parent if parent is not None else self.mainWindow)
        shortcut.setObjectName("StudioShortcut")
        shortcut.setContext(context)
        shortcut.setProperty("studioShortcut", True)
        shortcut.setProperty("studioDescription", description)
        shortcut.connect("activated()", callback)
        return shortcut

    def _addShortcut(self, keySequence, callback, description, parent=None, context=qt.Qt.WindowShortcut):
        shortcut = self._createShortcut(keySequence, callback, description, parent, context)
        self._shortcuts.append(shortcut)
        return shortcut

    @staticmethod
    def _deleteShortcut(shortcut):
        try:
            shortcut.setEnabled(False)
            shortcut.setObjectName("")
            shortcut.setProperty("studioShortcut", False)
            shortcut.deleteLater()
        except Exception:
            # Already deleted with its parent
            pass

    def shortcuts(self):
        """QShortcuts installed by Studio (global and for the current workspace)."""
        return list(self._shortcuts) + list(self._installedWorkspaceShortcuts)

    def addWorkspaceShortcut(self, wsKey, keySequence, callback, description=""):
        """Register a main window shortcut that is only active in the workspace ``wsKey``.

        It is installed when the workspace is entered and removed when it is left.
        """
        ws = workspaces.workspace(wsKey)
        if ws is None:
            raise ValueError(f"Unknown Studio workspace: {wsKey}")
        entries = self._workspaceShortcuts.setdefault(ws.key, [])
        entries.append((keySequence, callback, description))
        if self._active and self.currentWorkspace() == ws.key:
            self._installedWorkspaceShortcuts.append(self._createShortcut(keySequence, callback, description))

    def removeWorkspaceShortcut(self, wsKey, keySequence):
        ws = workspaces.workspace(wsKey)
        if ws is None:
            return
        portable = qt.QKeySequence(keySequence).toString()
        self._workspaceShortcuts[ws.key] = [entry for entry in self._workspaceShortcuts.get(ws.key, []) if qt.QKeySequence(entry[0]).toString() != portable]
        for shortcut in list(self._installedWorkspaceShortcuts):
            if shortcut.key.toString() == portable:
                self._installedWorkspaceShortcuts.remove(shortcut)
                self._deleteShortcut(shortcut)

    #
    # Workspaces
    #

    def switchTo(self, key, keepLayout=False):
        """Show the workspace ``key`` ("library", "draw" or "map"), in its remembered layout unless ``keepLayout``."""
        if not self._active:
            return
        self.workspaces.switchTo(key, keepLayout=keepLayout)

    def closeTransientModule(self):
        """Return from a transient module to the workspace it was opened from (the "← Draw" button, Esc)."""
        if not self._active:
            return
        origin = self._transientOrigin
        if origin is not None and self._availableWorkspaceKey(origin) is not None:
            self.switchTo(origin)
        else:
            self.workspaces.returnToLastWorkspace()

    def transientOrigin(self):
        """Key of the workspace that the shown transient module was opened from, None in a workspace."""
        return self._transientOrigin if self._active and self.currentWorkspace() is None else None

    def _onWorkspaceEntered(self, ws, shrinkPanel):
        self._transientOrigin = None
        self.topBar.setCurrentWorkspace(ws.key)
        self.panelHeader.setTitle(ws.title, caption=getattr(ws, "caption", None))
        for keySequence, callback, description in self._workspaceShortcuts.get(ws.key, []):
            self._installedWorkspaceShortcuts.append(self._createShortcut(keySequence, callback, description))
        self._showInspectorForModule()
        self._schedulePanelWidthUpdate(shrink=shrinkPanel)
        self._updateInspectorNotice()
        # The open project is saved when Library is shown (A-09)
        projectsUi = sys.modules.get("StudioLib.projects_ui")
        if projectsUi is not None:
            projectsUi.onWorkspaceEntered(ws.key)

    def _onCurrentWorkspaceReselected(self, ws):
        """The tab or the shortcut of the current workspace was used: bring the workspace back into view.

        The focus mode is left and an inspector that the user hid is shown again. An inspector that the
        workspace page hid itself (the Library empty state) stays hidden.
        """
        if self.focusMode():
            self.setFocusMode(False)
        if self._inspectorHiddenByUser and not self.isInspectorVisible():
            self.setInspectorVisible(True)

    def _onWorkspaceLeft(self, ws):
        for shortcut in self._installedWorkspaceShortcuts:
            self._deleteShortcut(shortcut)
        self._installedWorkspaceShortcuts = []
        self._primaryAction = None
        self._primaryActionLabel = ""
        self.setInspectorFooter(None)
        # A notice of the workspace stays registered and comes back with it
        self._updateInspectorNotice(leaving=ws.key)

    def _onTransientModuleEntered(self, moduleName):
        module = slicer.app.moduleManager().module(moduleName) if workspaces.moduleExists(moduleName) else None
        title = module.title if module is not None else moduleName
        self.topBar.setCurrentWorkspace(None)
        # The origin is recorded once per chain: Draw -> Volumes -> Data still goes back to Draw
        if self._transientOrigin is None:
            self._transientOrigin = self.workspaces.last or "library"
        origin = workspaces.workspace(self._transientOrigin) or workspaces.workspace("library")
        self.panelHeader.setTitle(title, transient=True, backText=origin.title, closeToolTip=ui.tooltip(
            _("Back to {workspace}").format(workspace=origin.title), "Esc", _("Close {module} and return").format(module=title)))
        self._showInspectorForModule()
        self._updateInspectorNotice()
        if moduleName == "DICOM":
            # Now if the module widget exists, else once the module panel has created it
            self._fitDicomBrowser()
            qt.QTimer.singleShot(0, self._fitDicomBrowser)
        # The keyboard focus starts on the module panel itself (neutral for Tab and Esc), not on its first input
        qt.QTimer.singleShot(0, lambda moduleName=moduleName: self._focusTransientModule(moduleName))

    #: Messages of the DICOM browser (CTK) that hold a folder path: one line, as wide as the path
    _DICOM_BROWSER_MESSAGE_LABELS = ("DatabaseDirectoryProblemLabel", "InformationMessageLabel")

    def _fitDicomBrowser(self):
        """Keep the DICOM browser from widening the window beyond the screen (1366 x 768).

        On a fresh profile its message "No valid DICOM database found in folder <path>" is one line as wide as
        the path, and the browser fills the views area: the window grew and pushed the inspector off the screen.
        The message labels wrap and give up their minimum width (a path that does not fit is cut at the right
        edge; the full text is in the tooltip). CTK is not changed; the labels stay so for the session.
        """
        if not self._active:
            return
        widget = getattr(slicer.modules, "DICOMWidget", None)
        browser = getattr(getattr(widget, "browserWidget", None), "dicomBrowser", None)
        if browser is None or getattr(self, "_dicomBrowserFitted", None) is browser:
            return
        self._dicomBrowserFitted = browser
        for name in self._DICOM_BROWSER_MESSAGE_LABELS:
            labels = slicer.util.findChildren(browser, name=name)
            label = labels[0] if labels else None
            if label is None or label.property("studioFitted"):
                continue
            label.setProperty("studioFitted", True)
            # Then word wrap, which keeps the policy and adds height-for-width (the frame grows a line)
            label.setSizePolicy(qt.QSizePolicy.Ignored, qt.QSizePolicy.Preferred)
            label.setWordWrap(True)
            label.installEventFilter(self._dicomLabelTooltipFilter())
            label.updateGeometry()
        # The window may have grown already (the browser was laid out before): back within the screen, and the
        # inspector width taken by the browser back to the inspector
        self._scheduleKeepWindowOnScreen()
        self._schedulePanelWidthUpdate(shrink=False)

    def _dicomLabelTooltipFilter(self):
        """Event filter that shows the full text of a DICOM browser message as its tooltip."""
        if getattr(self, "_dicomTooltipFilter", None) is None:
            self._dicomTooltipFilter = _LabelTextToolTipFilter()
        return self._dicomTooltipFilter

    def _focusTransientModule(self, moduleName):
        if not self._active or self.workspaces is None or self.workspaces.transientModule != moduleName:
            return
        modulePanel = self._modulePanel()
        if modulePanel is not None and modulePanel.isVisible():
            modulePanel.setFocus(qt.Qt.OtherFocusReason)

    def _showInspectorForModule(self):
        # Selecting a module shows the module panel (qSlicerMainWindow): in focus mode, leave the
        # focus mode rather than showing the inspector without the top bar
        if self.focusMode():
            self.setFocusMode(False)
        self.setInspectorVisible(True)

    def _onModuleAdded(self, moduleName):
        if moduleName == "SegmentEditor":
            self._setSegmentEditorPresentation("studio")
        elif moduleName == "LesionNetworkMapping":
            self._setMapPresentation(True)
        self._fitTextBrowsers()
        # A transient module may need a wider panel than the stored width
        self._schedulePanelWidthUpdate(shrink=False)

    def _prewarm(self, moduleName):
        if not self._active or not workspaces.moduleExists(moduleName):
            return
        module = slicer.app.moduleManager().module(moduleName)
        try:
            module.widgetRepresentation()
        except Exception:
            logging.error(f"Studio: cannot create the {moduleName} module widget:\n{traceback.format_exc()}")
        if moduleName == "SegmentEditor":
            self._setSegmentEditorPresentation("studio")
        elif moduleName == "LesionNetworkMapping":
            self._setMapPresentation(True)

    @staticmethod
    def _setMapPresentation(studio):
        """The Studio look of the Map panel (seed modes above the steps, compact headers), set before the inspector width
        is measured for Map; ``False`` gives the Classic look back.
        """
        widget = getattr(slicer.modules, "LesionNetworkMappingWidget", None)
        if widget is not None and hasattr(widget, "setStudioPresentation"):
            try:
                widget.setStudioPresentation(bool(studio), refresh=False)
            except Exception:
                logging.error(f"Studio: cannot set the look of Map:\n{traceback.format_exc()}")

    @staticmethod
    def _setSegmentEditorPresentation(name):
        widget = getattr(slicer.modules, "SegmentEditorWidget", None)
        if widget is not None and hasattr(widget, "setPresentation"):
            try:
                widget.setPresentation(name)
            except Exception:
                logging.error(f"Studio: cannot set the Segment Editor presentation:\n{traceback.format_exc()}")

    def setLayoutFromPicker(self, key):
        """Apply a layout of the top bar picker ("focus", "single", "fourup" or "row")."""
        layoutManager = slicer.app.layoutManager()
        if layoutManager is not None:
            layoutManager.setLayout(layouts.pickerLayoutId(key, self.hoverTracker))

    #
    # Plug-ins and lazily imported pages
    #

    def _installPlugins(self):
        self._plugins = []
        for name in OPTIONAL_PLUGINS:
            module = self._importOptional(name)
            if module is None or not hasattr(module, "install"):
                continue
            try:
                module.install(self)
                self._plugins.append(module)
            except Exception:
                logging.error(f"Studio: {name}.install() failed:\n{traceback.format_exc()}")

    @staticmethod
    def _pauseLesionQc():
        """The Lesion card (Draw) outlives Studio: its QC evaluation pending (debounce timer) must not run in Classic."""
        draw = sys.modules.get("StudioLib.draw")
        card = draw.lesionCard() if draw is not None else None
        qc = getattr(card, "qc", None)
        if qc is not None:
            try:
                qc.pause()
            except Exception:
                logging.error(f"Studio: the lesion QC could not be paused:\n{traceback.format_exc()}")

    def _uninstallPlugins(self):
        for module in reversed(self._plugins):
            try:
                if hasattr(module, "uninstall"):
                    module.uninstall(self)
            except Exception:
                logging.error(f"Studio: {module.__name__}.uninstall() failed:\n{traceback.format_exc()}")
        self._plugins = []

    @staticmethod
    def _importOptional(name):
        """Import a Studio module that may not exist in this build (None if it does not)."""
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name != name:
                logging.error(f"Studio: cannot import {name}:\n{traceback.format_exc()}")
        except Exception:
            logging.error(f"Studio: cannot import {name}:\n{traceback.format_exc()}")
        return None

    def openFiles(self):
        """Open files (Ctrl+O): the Studio direct-load dialog if available, else Add Data."""
        importer = self._importOptional("StudioLib.importer")
        if importer is not None and hasattr(importer, "openFilesDialog"):
            importer.openFilesDialog(self)
            return
        action = self._fileAddDataAction()
        if action is not None:
            action.trigger()
        else:
            slicer.util.openAddDataDialog()

    def openPalette(self, category=None):
        """Search (Ctrl+K; Ctrl+F filtered to the modules with ``category="modules"``): the command palette, or the
        module finder if the palette is not available.
        """
        palette = self._importOptional("StudioLib.palette")
        if palette is not None and hasattr(palette, "open"):
            try:
                palette.open(self, category=category)
            except TypeError:
                # A palette without categories
                palette.open(self)
            return
        self.mainWindow.moduleSelector().showModuleFinder()

    def toggleCheatSheet(self):
        """Keyboard shortcut overlay (Ctrl+/)."""
        cheatsheet = self._importOptional("StudioLib.cheatsheet")
        if cheatsheet is not None and hasattr(cheatsheet, "toggle"):
            cheatsheet.toggle(self)
            return
        self.toast(_("Ctrl+K search · Ctrl+Alt+1 Library, 2 Draw, 3 Map · Alt+1..4 layouts · {inspector} inspector · {panels} hide panels · "
                     "F10 menu · F6 regions · Ctrl+Alt+0 Classic").format(
            inspector=self.inspectorShortcutText(alternate=True), panels=focusModeKeyText()), 6000)

    #
    # Keyboard access: menus and focus regions
    #

    def showAppMenu(self, subMenuName=None):
        """Open the ≡ menu (F10), and with ``subMenuName`` ("FileMenu", "EditMenu", "ViewMenu", "HelpMenu") that submenu."""
        if not self._active or self.topBar is None:
            return
        if slicer.app.activePopupWidget() is not None or slicer.app.activeModalWidget() is not None:
            return
        menu = self.topBar.appMenu.menu
        button = self.topBar.menuButton
        if button.isVisible():
            point = button.mapToGlobal(qt.QPoint(0, button.height))
        else:
            # Hidden panels: the top-left corner of the views
            point = self.mainWindow.centralWidget().mapToGlobal(qt.QPoint(8, 8))
        menu.popup(point)
        if subMenuName:
            qt.QTimer.singleShot(0, lambda: self._openAppSubMenu(subMenuName))

    def _openAppSubMenu(self, subMenuName):
        menu = self.topBar.appMenu.menu if self.topBar is not None else None
        subMenu = self.mainWindow.findChild("QMenu", subMenuName)
        if menu is None or subMenu is None or not menu.isVisible():
            return
        subMenuAction = subMenu.menuAction()
        if subMenuAction not in menu.actions():
            return
        menu.setActiveAction(subMenuAction)
        if not subMenu.isVisible():
            geometry = menu.actionGeometry(subMenuAction)
            subMenu.popup(menu.mapToGlobal(geometry.topRight()))

    def _onAltKey(self, event):
        """A bare Alt press and release opens the ≡ menu on Windows and Linux (like a menu bar)."""
        eventType = event.type()
        key = event.key()
        if eventType in (qt.QEvent.ShortcutOverride, qt.QEvent.KeyPress):
            if key != qt.Qt.Key_Alt:
                self._cancelAltKey()
                return
            if eventType == qt.QEvent.KeyPress and self._altPending:
                return
            if isMacOS() or event.isAutoRepeat() or not self._altKeyMayOpenMenu() or self._otherModifiersHeld(event):
                self._cancelAltKey()
                return
            self._altPending = True
            self._altSeenHeld = False
            if self._altTimer is None:
                # Key releases do not always reach the main window (a view takes them): poll the modifier state
                self._altTimer = qt.QTimer()
                self._altTimer.setInterval(30)
                self._altTimer.connect("timeout()", self._pollAltKey)
            self._altStart = time.perf_counter()
            self._altTimer.start()
        elif eventType == qt.QEvent.KeyRelease and key == qt.Qt.Key_Alt:
            self._onAltReleased()

    @staticmethod
    def _otherModifiersHeld(event):
        """True if a modifier other than Alt is held with the Alt press (Ctrl+Alt, AltGr on Windows, Shift+Alt)."""
        others = qt.Qt.ControlModifier, qt.Qt.ShiftModifier, qt.Qt.MetaModifier
        try:
            modifiers = int(event.modifiers())
            return any(modifiers & int(modifier) for modifier in others)
        except (TypeError, ValueError):
            return any(event.modifiers() & modifier for modifier in others)

    def _altKeyMayOpenMenu(self):
        if slicer.app.activePopupWidget() is not None or slicer.app.activeModalWidget() is not None:
            return False
        return not _isTextInput(slicer.app.focusWidget())

    def _pollAltKey(self):
        if not self._altPending or not self._active:
            self._cancelAltKey()
            return
        try:
            mousePressed = int(qt.QApplication.mouseButtons()) != 0
        except (TypeError, ValueError):
            mousePressed = qt.QApplication.mouseButtons() != qt.Qt.NoButton
        if mousePressed or time.perf_counter() - self._altStart > 5.0:
            self._cancelAltKey()
            return
        try:
            held = bool(int(qt.QGuiApplication.queryKeyboardModifiers()) & int(qt.Qt.AltModifier))
        except (TypeError, ValueError):
            held = bool(qt.QGuiApplication.queryKeyboardModifiers() & qt.Qt.AltModifier)
        if held:
            self._altSeenHeld = True
        elif self._altSeenHeld:
            # Released (only trusted once the platform reported it held: synthetic events do not change it)
            self._onAltReleased()

    def _onAltReleased(self):
        pending = self._altPending
        self._cancelAltKey()
        active = self.mainWindow.isActiveWindow
        if callable(active):
            # A method when the property is not wrapped as an attribute
            active = active()
        if pending and self._active and active and self._altKeyMayOpenMenu():
            qt.QTimer.singleShot(0, self.showAppMenu)

    def _cancelAltKey(self):
        self._altPending = False
        self._altSeenHeld = False
        if self._altTimer is not None:
            self._altTimer.stop()

    def _focusRegions(self):
        """The keyboard focus regions in F6 order: [(name, container widget, function that focuses it)]."""
        regions = []
        if self.topBar is not None and self.topBar.toolBar.isVisible():
            regions.append(("topBar", self.topBar.toolBar, lambda: self.topBar.menuButton.setFocus(qt.Qt.ShortcutFocusReason)))
        dock = self._panelDock()
        if dock is not None and dock.isVisible():
            regions.append(("inspector", dock, self._focusInspector))
        canvas = self._canvas
        if canvas is not None and self._isAlive(canvas) and canvas.isVisible():
            regions.append(("canvas", canvas, self._focusCanvas))
        else:
            layoutManager = slicer.app.layoutManager()
            viewport = layoutManager.viewport() if layoutManager is not None else None
            if viewport is not None and viewport.isVisible():
                regions.append(("view", viewport, self._focusView))
        return regions

    def focusRegion(self):
        """Name of the region that has the keyboard focus ("topBar", "inspector", "view", "canvas"), or None."""
        focusWidget = slicer.app.focusWidget()
        if focusWidget is None:
            return None
        for name, container, focus in self._focusRegions():
            if container == focusWidget or container.isAncestorOf(focusWidget):
                return name
        return None

    def cycleFocusRegion(self, step=1):
        """Move the keyboard focus to the next (``step=1``, F6) or previous (-1, Shift+F6) region of the window."""
        if not self._active:
            return
        regions = self._focusRegions()
        if not regions:
            return
        current = self.focusRegion()
        names = [name for name, container, focus in regions]
        index = (names.index(current) + step) % len(regions) if current in names else (0 if step > 0 else len(regions) - 1)
        regions[index][2]()

    def _focusInspector(self):
        target = self.panelHeader.focusTarget() if self.panelHeader is not None else None
        modulePanel = self._modulePanel()
        if target is None and modulePanel is not None:
            target = self._firstTabStop(modulePanel)
        if target is None:
            target = modulePanel
        if target is not None:
            target.setFocus(qt.Qt.ShortcutFocusReason)

    @staticmethod
    def _firstTabStop(container, maximumSteps=2000):
        """The first visible and enabled widget of ``container`` that Tab can reach, or None."""
        widget = container.nextInFocusChain()
        for _step in range(maximumSteps):
            if widget is None or widget == container:
                return None
            try:
                tabFocus = bool(int(widget.focusPolicy) & int(qt.Qt.TabFocus))
            except (TypeError, ValueError):
                tabFocus = widget.focusPolicy in (qt.Qt.TabFocus, qt.Qt.StrongFocus, qt.Qt.WheelFocus)
            if tabFocus and widget.isVisible() and widget.isEnabled() and container.isAncestorOf(widget):
                return widget
            widget = widget.nextInFocusChain()
        return None

    def _focusCanvas(self):
        canvas = self._canvas
        library = self._importOptional("StudioLib.library") if self.currentWorkspace() == "library" else None
        page = library.canvas() if library is not None and hasattr(library, "canvas") else None
        if page is not None and getattr(page, "widget", None) == canvas and hasattr(page, "focusFirst"):
            page.focusFirst()
            return
        target = self._firstTabStop(canvas)
        (target or canvas).setFocus(qt.Qt.ShortcutFocusReason)

    def _focusView(self):
        layoutManager = slicer.app.layoutManager()
        if layoutManager is None:
            return
        names = list(layoutManager.sliceViewNames())
        name = self.hoverTracker.lastSliceViewName if self.hoverTracker is not None else None
        if name not in names:
            name = "Red" if "Red" in names else (names[0] if names else None)
        sliceWidget = layoutManager.sliceWidget(name) if name else None
        if sliceWidget is None:
            return
        sliceView = sliceWidget.sliceView()
        vtkWidget = sliceView.VTKWidget() if hasattr(sliceView, "VTKWidget") else None
        (vtkWidget or sliceView).setFocus(qt.Qt.ShortcutFocusReason)

    @staticmethod
    def _isAlive(widget):
        try:
            widget.objectName
        except Exception:
            return False
        return True

    #
    # Inspector
    #

    @property
    def panelWidth(self):
        """Width of the inspector (300-480 px, persisted in Studio/PanelWidth)."""
        width = slicer.util.settingsValue("Studio/PanelWidth", DEFAULT_PANEL_WIDTH, converter=int)
        return min(max(width, MIN_PANEL_WIDTH), MAX_PANEL_WIDTH)

    @panelWidth.setter
    def panelWidth(self, width):
        width = min(max(int(width), MIN_PANEL_WIDTH), MAX_PANEL_WIDTH)
        qt.QSettings().setValue("Studio/PanelWidth", width)
        if self._active:
            self._updatePanelWidth(shrink=True)

    def resetPanelWidth(self):
        self.panelWidth = DEFAULT_PANEL_WIDTH

    def _schedulePanelWidthUpdate(self, shrink):
        """Update the inspector width when the event loop runs again (the module widget is laid out
        first). Several requests are coalesced into one update; ``shrink`` is kept if any asked for it.
        """
        self._pendingPanelShrink = self._pendingPanelShrink or shrink
        if self._panelUpdateTimer is None:
            self._panelUpdateTimer = qt.QTimer()
            self._panelUpdateTimer.setSingleShot(True)
            self._panelUpdateTimer.setInterval(0)
            self._panelUpdateTimer.connect("timeout()", self._onPanelUpdateTimeout)
        self._panelUpdateTimer.start()

    def _onPanelUpdateTimeout(self):
        shrink = self._pendingPanelShrink
        self._pendingPanelShrink = False
        self._updatePanelWidth(shrink)

    def _updatePanelWidth(self, shrink=False):
        """Size the inspector to the stored width, or to the width that its module needs (SPEC 2.5).

        The module panel has an explicit minimum width and wider contents scroll horizontally
        (``_setModulePanelConstrained()``): the inspector width does not follow every change of the
        module widget. Instead:

        - The workspaces share one width: the stored width, or the widest layout minimum that a workspace
          module needed so far (``_workspacePanelWidth()``, kept across sessions), and never narrower. Once
          every tool was used, choosing a tool or a workspace does not move the views under the cursor,
          and the options are not clipped. The user cannot drag the inspector narrower than that either.
        - A transient module may make the inspector as wide as its widget needs.

        With ``shrink`` (entering a workspace, setting the width, showing the inspector again) the
        inspector gets back to that width; otherwise it only widens.
        """
        if not self._active:
            return
        dock = self._panelDock()
        if dock.isHidden():
            return
        contents = dock.widget()
        target = self.panelWidth
        needed = self._modulePanelNeededWidth()
        workspace = self.currentWorkspace()
        known = self._workspacePanelWidths.get(self.currentModuleName()) if workspace is not None else dock.width
        if known is None or needed > known:
            # A wider measurement is kept (a workspace's for good): measure again once the pending layouts (a module
            # widget just shown, a card that changed its look) are done, so that a transient layout never widens the
            # inspector. Only then: laying out every pending widget at each module switch would slow it down
            qt.QCoreApplication.sendPostedEvents(None, qt.QEvent.LayoutRequest)
            needed = self._modulePanelNeededWidth()
        contentsMinimumWidth = _MODULE_PANEL_MINIMUM_WIDTH
        if workspace is None:
            target = max(target, needed)
        else:
            self._workspacePanelWidth(self.currentModuleName(), min(needed, MAX_WORKSPACE_PANEL_WIDTH))
            # The workspaces share one width, so that switching between them does not resize (and render
            # again) the views
            held = max(self._workspacePanelWidths.values(), default=0)
            target = max(target, held)
            # The dock widget is as wide as its contents plus its margins
            contentsMinimumWidth = max(contentsMinimumWidth, held - max(0, dock.width - contents.width))
        if contents.minimumWidth != contentsMinimumWidth:
            contents.setMinimumWidth(contentsMinimumWidth)
        if dock.width < target or (shrink and dock.width != target):
            self.mainWindow.resizeDocks([dock], [target], qt.Qt.Horizontal)

    def _modulePanelNeededWidth(self):
        """Width of the inspector at which the module widget is not clipped (its layout minimum).

        Room for the vertical scroll bar is always kept, so that a module widget getting taller than the
        inspector does not widen it.
        """
        dock = self._panelDock()
        modulePanel = self._modulePanel()
        scrollArea = modulePanel.findChild("QScrollArea", "ScrollArea") if modulePanel is not None else None
        if scrollArea is None or scrollArea.widget() is None:
            # The minimum size hint of the module panel is the width of the module widget (qSlicerModulePanel)
            return modulePanel.minimumSizeHint.width() + max(0, dock.width - modulePanel.width) if modulePanel is not None else 0
        chrome = max(0, dock.width - scrollArea.width) + 2 * scrollArea.frameWidth + scrollArea.verticalScrollBar().sizeHint.width()
        return scrollArea.widget().minimumSizeHint.width() + chrome

    def _workspacePanelWidth(self, moduleName, needed):
        """Widest inspector width that the workspace module ``moduleName`` needed so far, at least ``needed``.

        Kept in ``Studio/WorkspacePanelWidths/<module name>`` with a signature of what the layout depends on
        (``_layoutSignature()``): another Slicer build or application font measures again.
        """
        if not moduleName:
            return needed
        held = self._workspacePanelWidths.get(moduleName)
        key = f"{_WORKSPACE_PANEL_WIDTHS_GROUP}/{moduleName}"
        if held is None:
            held = 0
            stored = str(qt.QSettings().value(key, "") or "")
            width, _separator, signature = stored.partition("@")
            if signature == self._layoutSignature():
                try:
                    held = min(max(int(width), 0), MAX_WORKSPACE_PANEL_WIDTH)
                except ValueError:
                    held = 0
        if needed > held:
            held = needed
            qt.QSettings().setValue(key, f"{held}@{self._layoutSignature()}")
        self._workspacePanelWidths[moduleName] = held
        return held

    @staticmethod
    def _layoutSignature():
        """What the layout minimum of a module widget depends on: the Slicer build and the application font."""
        font = slicer.app.font()
        return f"{slicer.app.applicationVersion}-{slicer.app.revision}-{font.family()}-{font.pointSizeF()}-{font.pixelSize()}"

    def resetWorkspacePanelWidths(self):
        """Forget the widths needed by the workspace modules (they are measured again as tools are used)."""
        self._workspacePanelWidths = {}
        qt.QSettings().remove(_WORKSPACE_PANEL_WIDTHS_GROUP)
        if self._active:
            self._updatePanelWidth(shrink=True)

    def _setModulePanelConstrained(self, constrained):
        """Keep the inspector width independent of the width of the module widget (Studio), or restore Classic.

        The module panel reports the width of the module widget as its minimum size hint, which sets the
        minimum width of the dock widget: selecting a tool with wide options (Paint: 425 -> 550 px) widened
        the inspector and shrank the views. In Studio the module panel gets an explicit minimum width and
        its scroll area a horizontal scroll bar when needed.
        """
        modulePanel = self._modulePanel()
        if modulePanel is None:
            return
        # The dock widget contents (module panel, footer, Data Probe) report the module widget width as
        # their minimum size too (their size policy does not shrink below the size hint)
        contents = self._panelDock().widget()
        scrollArea = modulePanel.findChild("QScrollArea", "ScrollArea")
        if constrained:
            if self._classicModulePanelState is None:
                self._classicModulePanelState = (modulePanel.minimumWidth, contents.minimumWidth,
                                                 scrollArea.horizontalScrollBarPolicy if scrollArea is not None else None)
            modulePanel.setMinimumWidth(_MODULE_PANEL_MINIMUM_WIDTH)
            contents.setMinimumWidth(_MODULE_PANEL_MINIMUM_WIDTH)
            if scrollArea is not None:
                scrollArea.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAsNeeded)
                # A tool chosen in the module widget changes its layout minimum: see _updatePanelWidth()
                if self._eventFilter is not None and scrollArea.widget() is not None and self._watchedPanelContents is None:
                    self._watchedPanelContents = scrollArea.widget()
                    self._watchedPanelContents.installEventFilter(self._eventFilter)
            self._fitTextBrowsers()
            return
        self._unfitTextBrowsers()
        if self._classicModulePanelState is not None:
            modulePanelMinimumWidth, contentsMinimumWidth, policy = self._classicModulePanelState
            modulePanel.setMinimumWidth(modulePanelMinimumWidth)
            contents.setMinimumWidth(contentsMinimumWidth)
            if scrollArea is not None and policy is not None:
                scrollArea.setHorizontalScrollBarPolicy(policy)
            self._classicModulePanelState = None

    def _fitTextBrowsers(self):
        """Give the fitted text browsers of the module panel (the Segment Editor effect help) an explicit
        minimum width. Their minimum size hint is their current width, so that they never let the panel
        get narrower once it was wide.
        """
        if not self._active:
            return
        modulePanel = self._modulePanel()
        if modulePanel is None:
            return
        for browser in modulePanel.findChildren("ctkFittedTextBrowser"):
            if browser.minimumWidth == 0 and browser not in self._fittedTextBrowsers:
                browser.setMinimumWidth(1)
                self._fittedTextBrowsers.append(browser)

    def _unfitTextBrowsers(self):
        for browser in self._fittedTextBrowsers:
            try:
                if browser.minimumWidth == 1:
                    browser.setMinimumWidth(0)
            except Exception:
                # Deleted with its module widget
                pass
        self._fittedTextBrowsers = []

    def _onDockResized(self):
        # Persist the width when the user drags the separator between the views and the inspector
        if not (qt.QApplication.mouseButtons() & qt.Qt.LeftButton):
            return
        if self._panelWidthTimer is None:
            self._panelWidthTimer = qt.QTimer()
            self._panelWidthTimer.setSingleShot(True)
            self._panelWidthTimer.setInterval(300)
            self._panelWidthTimer.connect("timeout()", self._persistDraggedPanelWidth)
        self._panelWidthTimer.start()

    def _persistDraggedPanelWidth(self):
        if not self._active:
            return
        dock = self._panelDock()
        width = min(max(dock.width, MIN_PANEL_WIDTH), MAX_PANEL_WIDTH)
        if width != self.panelWidth:
            qt.QSettings().setValue("Studio/PanelWidth", width)
        if width != dock.width:
            # Dragged out of the 300-480 px range: snap back into it (but not narrower than a workspace
            # module needs, see _updatePanelWidth())
            self._updatePanelWidth(shrink=True)

    def setInspectorVisible(self, visible):
        """Show or hide the inspector (the module panel dock widget)."""
        if not self._active:
            return
        if visible:
            self._inspectorHiddenByUser = False
        dock = self._panelDock()
        if dock.isHidden() == (not visible):
            self._updateInspectorHandle()
            return
        # When shown, the width is updated on its Show event
        dock.setVisible(visible)
        self._updateInspectorHandle()

    def isInspectorVisible(self):
        return self._active and not self._panelDock().isHidden()

    def toggleInspector(self):
        r"""Show or hide the inspector (``Ctrl+\``, the app menu). Hiding it shows how to get it back."""
        if not self._active:
            return
        if self.focusMode():
            # The inspector is hidden by the focus mode: leave it
            self.setFocusMode(False)
            self.setInspectorVisible(True)
            return
        visible = not self.isInspectorVisible()
        self.setInspectorVisible(visible)
        if not visible:
            self._onInspectorHiddenByUser()

    def _onPanelToggleTriggered(self, checked):
        # View > Module Panel (Ctrl+5, the second inspector key) toggles the dock widget directly
        if checked:
            self._inspectorHiddenByUser = False
            if self.focusMode():
                # The top bar comes back with the inspector
                self.setFocusMode(False)
        else:
            self._onInspectorHiddenByUser()
        self._updateInspectorHandle()

    def _onInspectorHiddenByUser(self):
        """The user hid the inspector (a key or the menu): say how to get it back, each time (a consequence toast)."""
        self._inspectorHiddenByUser = True
        self._updateInspectorHandle()
        ws = workspaces.workspace(self.currentWorkspace() or self._transientOrigin)
        if ws is not None:
            text = _("Inspector hidden · click the {workspace} tab or the {handle} handle to show it").format(workspace=ws.title, handle="\u2039")
        else:
            text = _("Inspector hidden · click the {handle} handle to show it").format(handle="\u2039")
        self.toast(text, level="consequence", action=lambda: self.setInspectorVisible(True), actionText=_("Show"),
                   toolTip=ui.tooltip(_("Show inspector"), self.inspectorShortcutText(alternate=True)))

    def _inspectorHandleWanted(self):
        """The edge handle is shown while the inspector is hidden over visible views: not over the Library canvas
        (Library shows the inspector itself when it has something to show) and not with hidden panels (their pill).
        """
        if not self._active or self._inspectorHandle is None:
            return False
        if self._canvas is not None or self.focusMode():
            return False
        dock = self._panelDock()
        return dock is not None and dock.isHidden()

    def _updateInspectorHandle(self):
        if self._inspectorHandle is not None:
            self._inspectorHandle.setVisible(self._inspectorHandleWanted())

    def _updateInspectorHandlePosition(self):
        if self._inspectorHandle is not None:
            self._inspectorHandle.updatePosition()

    def inspectorHandle(self):
        """The edge handle button of the hidden inspector (``StudioInspectorHandle``), or None."""
        return self._inspectorHandle.button if self._inspectorHandle is not None else None

    @staticmethod
    def inspectorShortcutText(alternate=False):
        r"""The key that shows or hides the inspector, as displayed on this platform (for example "Ctrl+\").

        With ``alternate``, both keys ("Ctrl+\ or Ctrl+5"): not every keyboard layout can type Ctrl+\.
        """
        text = qt.QKeySequence(INSPECTOR_SHORTCUT).toString(qt.QKeySequence.NativeText)
        if not alternate:
            return text
        return _("{key} or {alternateKey}").format(
            key=text, alternateKey=qt.QKeySequence(INSPECTOR_ALTERNATE_SHORTCUT).toString(qt.QKeySequence.NativeText))

    def setInspectorFooter(self, widget=None):
        """Show ``widget`` in the sticky footer below the module panel (None: no footer)."""
        if self._footerSlot is None:
            return
        layout = self._footerSlot.layout()
        if self._footerWidget is not None and self._footerWidget != widget:
            try:
                # The owner of the footer widget keeps it (it is not deleted here)
                layout.removeWidget(self._footerWidget)
                self._footerWidget.hide()
                self._footerWidget.setParent(None)
            except Exception:
                # The previous footer widget was deleted by its owner
                pass
        self._footerWidget = widget
        if widget is not None:
            layout.addWidget(widget)
            widget.show()
        self._footerSlot.setVisible(widget is not None)

    def inspectorFooter(self):
        return self._footerWidget

    def setInspectorNotice(self, widget=None, key=None, workspace=None):
        """Show ``widget`` in the inspector notice slot, between the panel header and the module (USABILITY 6.4).

        Offers and notices (downloads, recovery, questions) go here, never over a view. The slot holds one
        notice: a new one replaces the current one, whose widget is hidden and given back to its owner (it is
        not deleted). With ``workspace`` (for example "draw") the notice is shown only while that workspace is
        current, and stays registered when it is left. ``widget=None`` removes the notice. Notices must be
        height-for-width (word-wrapped labels; ``ui.noticeLine()``, ``ui.wrapRow()``): the slot never widens
        the inspector.
        """
        if self._noticeSlot is None:
            return
        layout = self._noticeSlot.layout()
        previous = self._notice[0] if self._notice is not None else None
        if previous is not None and previous != widget:
            try:
                layout.removeWidget(previous)
                previous.hide()
                previous.setParent(None)
            except Exception:
                # Deleted by its owner
                pass
        if widget is None:
            self._notice = None
        else:
            wsKey = workspaces.workspace(workspace).key if workspace and workspaces.workspace(workspace) is not None else workspace
            self._notice = [widget, key, wsKey]
            if previous != widget:
                layout.addWidget(widget)
        self._updateInspectorNotice()

    def inspectorNotice(self):
        """The widget of the current inspector notice (also while its workspace is not current), or None."""
        return self._notice[0] if self._notice is not None else None

    def inspectorNoticeKey(self):
        return self._notice[1] if self._notice is not None else None

    def clearInspectorNotice(self, key):
        """Remove the inspector notice if it was set with ``key``. Return True if it was removed."""
        if self._notice is None or self._notice[1] != key:
            return False
        self.setInspectorNotice(None)
        return True

    def _updateInspectorNotice(self, leaving=None):
        if self._noticeSlot is None:
            return
        visible = False
        if self._notice is not None:
            widget, _key, wsKey = self._notice
            current = self.currentWorkspace()
            visible = wsKey is None or (wsKey == current and wsKey != leaving)
            try:
                widget.setVisible(visible)
            except Exception:
                # Deleted by its owner
                self._notice = None
                visible = False
        self._noticeSlot.setVisible(visible)

    def setDataProbeVisible(self, visible):
        dataProbe = self._dataProbe()
        if dataProbe is not None:
            dataProbe.setVisible(visible)

    #
    # Canvas
    #

    def setCanvas(self, widget=None):
        """Show ``widget`` over the view area (rendering of the views is paused), or hide the canvas (None)."""
        if self._canvas is not None and self._canvas != widget:
            try:
                self._canvas.hide()
            except Exception:
                # The canvas was deleted by its owner
                pass
        self._canvas = widget
        layoutManager = slicer.app.layoutManager()
        if widget is not None:
            centralWidget = self.mainWindow.centralWidget()
            if widget.parentWidget() != centralWidget:
                widget.setParent(centralWidget)
            widget.setGeometry(centralWidget.rect)
            widget.show()
            widget.raise_()
            if self._toast is not None and self._toast.isVisible():
                self._toast.label.raise_()
            if self._stickyToast is not None:
                self._stickyToast.raise_()
            if self._focusModePill is not None:
                self._focusModePill.raise_()
            if not self._renderPausedByCanvas and layoutManager is not None:
                layoutManager.setRenderPaused(True)
                self._renderPausedByCanvas = True
            self._holdViewAreaSize(layoutManager)
            # No Tab stop under the canvas (the views are covered, not disabled: that costs 27-55 ms)
            self._coverFocusPolicies()
        else:
            self._releaseViewAreaSize()
            if self._renderPausedByCanvas:
                if layoutManager is not None:
                    layoutManager.setRenderPaused(False)
                self._renderPausedByCanvas = False
            self._restoreCoveredFocusPolicies()
        self._updateInspectorHandle()

    def _holdViewAreaSize(self, layoutManager):
        """Keep the views at their size while the canvas covers them (it is released by ``_releaseViewAreaSize()``).

        The Library empty state hides the inspector, which widens the central widget: without this, every switch
        between Library and Draw resized the views twice (and re-created their render buffers and laid out their
        bars), although they are covered. The area that the views leave free is covered by the canvas. When the
        main window is resized, the views follow it again (``_onEvent()``).
        """
        if self._heldViewArea is not None:
            return
        viewport = layoutManager.viewport() if layoutManager is not None else None
        if viewport is None or not viewport.isVisible():
            return
        self._heldViewArea = (viewport, viewport.maximumSize)
        viewport.setMaximumSize(viewport.size)

    def _releaseViewAreaSize(self):
        if self._heldViewArea is None:
            return
        viewport, maximumSize = self._heldViewArea
        self._heldViewArea = None
        try:
            viewport.setMaximumSize(maximumSize)
        except Exception:
            # Deleted with the layout manager
            pass

    def canvas(self):
        return self._canvas

    def isRenderPausedByCanvas(self):
        return self._renderPausedByCanvas

    @staticmethod
    def _acceptsTab(widget):
        try:
            return bool(int(widget.focusPolicy) & int(qt.Qt.TabFocus))
        except (TypeError, ValueError):
            return widget.focusPolicy in (qt.Qt.TabFocus, qt.Qt.StrongFocus, qt.Qt.WheelFocus)

    def _coverFocusPolicies(self):
        """Take the widgets of the view area (views, view bars) out of the Tab chain while the canvas covers them.

        Their focus policies are stored and restored exactly by ``_restoreCoveredFocusPolicies()``. The view area
        has hundreds of widgets (about 600 with a 4-up layout): the ones whose policy accepts Tab are found once per
        layout (``_viewTabStops``, about 4 ms), and each call only covers those that are visible now (about 0.3 ms).
        Hidden widgets are not Tab stops. Visibility is checked on every call, so that a widget shown since the scan
        is covered too, and a call made while the main window is hidden (nothing visible) is made again when it is
        shown (``_onEvent``).
        """
        layoutManager = slicer.app.layoutManager()
        viewport = layoutManager.viewport() if layoutManager is not None else None
        if viewport is None:
            return
        covered = self._coveredFocusPolicies
        if self._viewTabStops is None:
            self._viewTabStops = [widget for widget in viewport.findChildren("QWidget")
                                  if id(widget) in covered or self._acceptsTab(widget)]
        for widget in self._viewTabStops:
            try:
                if id(widget) in covered or not widget.isVisible() or not self._acceptsTab(widget):
                    continue
                covered[id(widget)] = (widget, widget.focusPolicy)
                widget.setFocusPolicy(qt.Qt.NoFocus)
            except Exception:
                # Deleted with its view
                pass

    def _restoreCoveredFocusPolicies(self):
        covered = self._coveredFocusPolicies
        self._coveredFocusPolicies = {}
        for widget, policy in covered.values():
            try:
                if widget.focusPolicy == qt.Qt.NoFocus:
                    widget.setFocusPolicy(policy)
            except Exception:
                # Deleted with its view (layout change)
                pass

    def _onLayoutChanged(self, layoutId=None):
        # The views of the new layout are found again
        self._viewTabStops = None
        if layoutId == layouts.ROW_LAYOUT_ID:
            # "Side by side": the same width for the three views (the splitter gets its widths when the event loop runs)
            qt.QTimer.singleShot(0, layouts.equalizeRow)
        if self._inspectorHandle is not None and self._inspectorHandle.isVisible():
            # Keep the handle off the view bars of the new layout (placed once the views have their geometry)
            qt.QTimer.singleShot(0, self._updateInspectorHandlePosition)
        if self._canvas is not None:
            # View widgets created by the layout change are covered too
            self._coverFocusPolicies()

    #
    # Top bar
    #

    def toast(self, text, ms=1600, action=None, actionText=None, sticky=False, level="info", toolTip=None):
        """Show a message near the bottom of the view area (USABILITY 6.3) and list it in ``recentMessages()``.

        A confirmation ("Saved sub-01_lesion.nii.gz") fades out after ``ms`` milliseconds. A consequence of an
        automatic action that the user must know about (``level="consequence"`` or ``sticky=True``: a new lesion
        was created and the previous one hidden, the inspector was hidden...) stays until the next input (at
        least ``STICKY_TOAST_MINIMUM_MS``). Both may carry one ``action`` button labelled ``actionText``
        ("Undo", "Show", "Details"). A new message replaces the current one.
        """
        sticky = bool(sticky) or level == "consequence"
        self._recentMessages.append((time.time(), str(text), level if not sticky or level != "info" else "consequence"))
        if self._toast is None or self._stickyToast is None:
            return
        if sticky or action is not None:
            self._hidePlainToast()
            self._stickyToast.show(text, sticky=sticky, ms=ms, action=action, actionText=actionText, level=level)
            self._stickyToast.frame.setToolTip(toolTip or "")
            return
        self._stickyToast.hide()
        self._toast.show(text, ms, level=level)
        self._toast.label.setAccessibleName(str(text))

    def _hidePlainToast(self):
        toast = self._toast
        if toast is None:
            return
        for name in ("hideTimer", "fadeAnimation"):
            member = getattr(toast, name, None)
            if member is not None:
                member.stop()
        toast.label.hide()

    def _onMainWindowResized(self, event):
        """Note whether Qt grew the main window to its minimum size (then it may be corrected) or the user sized it."""
        size = event.size() if callable(getattr(event, "size", None)) else None
        oldSize = event.oldSize() if callable(getattr(event, "oldSize", None)) else None
        if size is None or oldSize is None or not oldSize.isValid():
            return
        if size == oldSize:
            return
        if (size.width(), size.height()) == self._windowResizedTo:
            # The size keepWindowOnScreen() asked for. Qt took the width from the inspector: give it back its
            # width (from the views, as far as their minimum width allows)
            self._windowResizedTo = None
            self._schedulePanelWidthUpdate(shrink=False)
            return
        minimum = self._mainWindowMinimumSize()
        grownWidth = size.width() > oldSize.width() and size.width() <= minimum.width()
        grownHeight = size.height() > oldSize.height() and size.height() <= minimum.height()
        if grownWidth or grownHeight:
            # setMinimumSize() made the window larger: Qt's doing, corrected when the event loop runs
            self._windowGrownForMinimum = True
            self._scheduleKeepWindowOnScreen()
        else:
            # A size the user chose: kept as it is
            self._windowGrownForMinimum = False

    def _mainWindowMinimumSize(self):
        mainWindow = self.mainWindow
        return mainWindow.minimumSizeHint.expandedTo(mainWindow.minimumSize)

    @staticmethod
    def _availableDesktop():
        """Region of the available geometries of all screens (a window may span two monitors), None if unknown."""
        region = qt.QRegion()
        for screen in qt.QGuiApplication.screens():
            screen = ui._validScreen(screen)
            if screen is None:
                continue
            available = screen.availableGeometry
            if callable(available):
                available = available()
            region = region.united(qt.QRegion(available))
        return None if region.isEmpty() else region

    def _scheduleKeepWindowOnScreen(self):
        if self._windowOnScreenTimer is None:
            self._windowOnScreenTimer = qt.QTimer()
            self._windowOnScreenTimer.setSingleShot(True)
            self._windowOnScreenTimer.setInterval(0)
            self._windowOnScreenTimer.connect("timeout()", self.keepWindowOnScreen)
        self._windowOnScreenTimer.start()

    def keepWindowOnScreen(self):
        """Bring a main window that Qt grew beyond the available area of the screens back within its screen, as far
        as its minimum size allows (USABILITY: stable layout; the inspector and the search box stay on the screen).

        Qt makes the window larger when the views of a layout (or a module) need more room than it has, and never
        makes it smaller again. Only that case is corrected, now and when the minimum size becomes smaller (another
        layout): a size or a position the user chose is kept, also across several monitors. A maximized, full-screen
        or minimized window is left alone. Return True if the window was resized or moved.
        """
        mainWindow = self.mainWindow
        if not self._windowGrownForMinimum:
            return False
        if not mainWindow.isVisible() or mainWindow.isMaximized() or mainWindow.isFullScreen() or mainWindow.isMinimized():
            return False
        frame = mainWindow.frameGeometry
        if callable(frame):
            frame = frame()
        desktop = self._availableDesktop()
        if desktop is not None and qt.QRegion(frame).subtracted(desktop).isEmpty():
            # Within the screens (possibly spanning two of them): nothing left to correct
            self._windowGrownForMinimum = False
            return False
        screen = ui.screenOf(mainWindow)
        if screen is None:
            return False
        available = screen.availableGeometry
        if callable(available):
            available = available()
        frameWidth = frame.width() - mainWindow.width
        frameHeight = frame.height() - mainWindow.height
        minimum = self._mainWindowMinimumSize()
        width = max(min(mainWindow.width, available.width() - frameWidth), minimum.width())
        height = max(min(mainWindow.height, available.height() - frameHeight), minimum.height())
        changed = False
        if width != mainWindow.width or height != mainWindow.height:
            self._windowResizedTo = (width, height)
            mainWindow.resize(width, height)
            changed = True
        frame = mainWindow.frameGeometry
        if callable(frame):
            frame = frame()
        x = max(available.x(), min(frame.x(), available.x() + available.width() - frame.width()))
        y = max(available.y(), min(frame.y(), available.y() + available.height() - frame.height()))
        if x != frame.x() or y != frame.y():
            self._windowMovedTo = (x, y)
            mainWindow.move(x, y)
            changed = True
        if width <= available.width() - frameWidth and height <= available.height() - frameHeight:
            # The window fits its screen now; until it does, a smaller minimum size (another layout) lets it come back
            self._windowGrownForMinimum = False
        return changed

    def _updateTopBarCompact(self):
        """Narrow mode of the top bar below ``NARROW_WINDOW_WIDTH`` (the window width, so it never oscillates)."""
        if self.topBar is None or not hasattr(self.topBar, "setCompact"):
            return
        self.topBar.setCompact(self.mainWindow.width < NARROW_WINDOW_WIDTH)
        # The autosave status has its own, wider thresholds (setCompact() updates it too when the mode changes)
        if hasattr(self.topBar, "updateSaveStatusCompact"):
            self.topBar.updateSaveStatusCompact()

    def toastText(self):
        """Text of the visible toast (plain or consequence), "" if none is shown."""
        if self._stickyToast is not None and self._stickyToast.isVisible():
            return self._stickyToast.text()
        if self._toast is not None and self._toast.isVisible():
            return self._toast.text()
        return ""

    def recentMessages(self):
        """The last toast messages, oldest first: [(time.time(), text, level)] (≡ > View > Recent messages)."""
        return list(self._recentMessages)

    def showRecentMessages(self):
        """Show the recent messages in a small read-only list popover (Esc or a click outside closes it)."""
        if not self._active:
            return
        menu = qt.QMenu(self.mainWindow)
        menu.setObjectName("StudioRecentMessagesPopover")
        listWidget = qt.QListWidget()
        listWidget.setObjectName("StudioRecentMessagesList")
        listWidget.setSelectionMode(qt.QAbstractItemView.NoSelection)
        listWidget.setFocusPolicy(qt.Qt.NoFocus)
        listWidget.setFrameShape(qt.QFrame.NoFrame)
        messages = self.recentMessages()
        for timestamp, text, level in reversed(messages):
            item = qt.QListWidgetItem(f"{time.strftime('%H:%M:%S', time.localtime(timestamp))}  {text}")
            listWidget.addItem(item)
        if not messages:
            listWidget.addItem(_("No messages yet"))
        listWidget.setMinimumWidth(360)
        listWidget.setFixedHeight(min(320, max(40, listWidget.sizeHintForRow(0) * max(1, len(messages)) + 8)))
        action = qt.QWidgetAction(menu)
        action.setDefaultWidget(listWidget)
        menu.addAction(action)
        button = self.topBar.menuButton if self.topBar is not None else None
        anchor = button if button is not None and button.isVisible() else self.mainWindow.centralWidget()
        _execMenu(menu, anchor.mapToGlobal(qt.QPoint(0, anchor.height if anchor == button else 8)))
        menu.deleteLater()

    def setBreadcrumb(self, primary, secondary=None, onClick=None):
        """Set the top bar breadcrumb ("study / subject"). ``primary=None`` shows the first volume name."""
        if self.topBar is not None:
            self.topBar.setBreadcrumb(primary, secondary, onClick)

    def setStatusChip(self, text=None, level="neutral"):
        """Show a status chip next to the workspace switcher (level: neutral, busy, ok, warn). None hides it."""
        if self.topBar is not None:
            self.topBar.setStatusChip(text, level)

    def setSaveStatus(self, text=None, level="neutral", toolTip="", iconName=None, title=None):
        """Show the save status in the top bar; level: neutral, busy, ok, warn. None hides it.

        Without a project it is the recovery autosave ("Autosaved · 2 min ago": autosave is not Save, SPEC 9.9); in a
        project it is the save state ("Saved", A-09). ``title`` names the status in its tooltip ("Autosave" by default).
        No-op without a top bar.
        """
        if self.topBar is not None and hasattr(self.topBar, "setSaveStatus"):
            self.topBar.setSaveStatus(text, level, toolTip, iconName, title=title)

    def setSaveStatusMenu(self, menu=None):
        """Set the menu that the autosave status opens (None: no menu). No-op without a top bar."""
        if self.topBar is not None and hasattr(self.topBar, "setSaveStatusMenu"):
            self.topBar.setSaveStatusMenu(menu)

    def setPrimaryAction(self, callback=None, label=""):
        """Set the action of Ctrl+Enter (the primary button of the current workspace)."""
        self._primaryAction = callback
        self._primaryActionLabel = label

    def primaryActionLabel(self):
        return self._primaryActionLabel if self._primaryAction is not None else ""

    def triggerPrimaryAction(self):
        if self._primaryAction is None:
            return
        try:
            self._primaryAction()
        except Exception:
            logging.error(f"Studio primary action failed:\n{traceback.format_exc()}")

    #
    # Focus mode
    #

    def focusMode(self):
        if hasattr(self.mainWindow, "focusMode"):
            # Slot or property of the main window (W3-03)
            focusMode = self.mainWindow.focusMode
            return bool(focusMode() if callable(focusMode) else focusMode)
        return self._focusModeHidden is not None

    def setFocusMode(self, enabled):
        """Hide (or show again) the inspector and the top bar: "Hide panels" (F11 or ⌘6), formerly "focus mode".

        With hidden panels a low-emphasis "Show panels" pill stays in the top-right corner of the
        views, and hiding them says how to show them again (the key or Esc).
        """
        wasFocusMode = self.focusMode()
        if hasattr(self.mainWindow, "setFocusMode"):
            if self._active or not enabled:
                self.mainWindow.setFocusMode(enabled)
        elif enabled != (self._focusModeHidden is not None):
            if enabled:
                if not self._active:
                    return
                widgets = [self._panelDock(), self.topBar.toolBar]
                self._focusModeHidden = [widget for widget in widgets if not widget.isHidden()]
                for widget in self._focusModeHidden:
                    widget.hide()
            else:
                for widget in self._focusModeHidden:
                    widget.show()
                self._focusModeHidden = None
        self._updateFocusModeIndicators(wasFocusMode)

    def _updateFocusModeIndicators(self, wasFocusMode):
        focusMode = self._active and self.focusMode()
        if self._focusModePill is not None:
            if focusMode:
                self._focusModePill.show()
            else:
                self._focusModePill.hide()
        self._updateInspectorHandle()
        text = _("Panels hidden · {key} or Esc to show them").format(key=focusModeKeyText())
        if focusMode and not wasFocusMode:
            self.toast(text, level="consequence", action=lambda: self.setFocusMode(False), actionText=_("Show panels"))
        elif wasFocusMode and not focusMode and self._stickyToast is not None and self._stickyToast.isVisible() and self._stickyToast.text() == text:
            self._stickyToast.hide()

    def toggleFocusMode(self):
        self.setFocusMode(not self.focusMode())

    #
    # Events
    #

    def addHelpLink(self, key, text, callback, isEnabled=None):
        """Add a link at the end of the help popover of the panel header (for example "Tour this workspace").

        ``callback()`` runs after the popover is closed; the link is shown only while ``isEnabled()`` (if given)
        returns True. A link with the same ``key`` is replaced.
        """
        self.removeHelpLink(key)
        self._helpLinks.append((key, text, callback, isEnabled))

    def removeHelpLink(self, key):
        self._helpLinks = [link for link in self._helpLinks if link[0] != key]

    def helpLinks(self):
        """The enabled links of the help popover: [(key, text)]."""
        links = []
        for key, text, callback, isEnabled in self._helpLinks:
            try:
                enabled = isEnabled is None or bool(isEnabled())
            except Exception:
                logging.error(f"Studio: help link {key} failed:\n{traceback.format_exc()}")
                enabled = False
            if enabled:
                links.append((key, text))
        return links

    def helpLinkCallback(self, key):
        for linkKey, text, callback, isEnabled in self._helpLinks:
            if linkKey == key:
                return callback
        return None

    def registerEscapeOverlay(self, widget, closeFn):
        """Register a Studio overlay that is not a Qt popup (the shortcut list, a popover) that Esc closes (rung 0.5).

        The overlays form a stack: Esc calls ``closeFn()`` of the top-most visible one (the last registered).
        Hidden overlays stay registered until ``unregisterEscapeOverlay(widget)``.
        """
        self.unregisterEscapeOverlay(widget)
        self._escapeOverlays.append((widget, closeFn))

    def unregisterEscapeOverlay(self, widget):
        self._escapeOverlays = [(overlay, closeFn) for overlay, closeFn in self._escapeOverlays if overlay != widget]

    def _closeTopEscapeOverlay(self):
        for overlay, closeFn in reversed(list(self._escapeOverlays)):
            try:
                visible = overlay.isVisible()
            except Exception:
                # Deleted by its owner
                self.unregisterEscapeOverlay(overlay)
                continue
            if not visible:
                continue
            try:
                closeFn()
            except Exception:
                logging.error(f"Studio: closing an overlay with Esc failed:\n{traceback.format_exc()}")
            return True
        return False

    def handleEscape(self):
        """Back out of the innermost state, as Esc does in Studio. Return True if a state was left.

        The Esc ladder (USABILITY 3, ``ESCAPE_LADDER``), one rung per key press:

        0.5. a registered Studio overlay (``registerEscapeOverlay()``) -> closed;
        1. Place mode or Adjust window/level mode of the mouse -> the default mode (view transform);
        2. a maximized view (double-click on a view, the view bar button, the Single layout) -> the layout is restored;
        3. hidden panels (F11 / ⌘6) -> shown;
        4. an active Segment Editor tool (Draw) -> not handled here: the key is not consumed, so that the Segment
           Editor's own Esc deselects the tool;
        5. a transient module -> back to the workspace it was opened from (``closeTransientModule()``).

        Before the ladder (rung 0), popups and dialogs close themselves, and consoles, editable text fields,
        line edits with unsent edits and open item editors keep their own Esc (see ``_onEscapeKey()``). Esc never
        stops a computation and never leaves a workspace: Library is outside the ladder.
        """
        if not self._active:
            return False
        if self._closeTopEscapeOverlay():
            return True
        modes = (slicer.vtkMRMLInteractionNode.Place, slicer.vtkMRMLInteractionNode.AdjustWindowLevel)
        handled = False
        for interactionNode in slicer.util.getNodesByClass("vtkMRMLInteractionNode"):
            if interactionNode.GetCurrentInteractionMode() in modes:
                interactionNode.SwitchToViewTransformMode()
                handled = True
        if handled:
            return True
        layoutManager = slicer.app.layoutManager()
        layoutNode = layoutManager.layoutLogic().GetLayoutNode() if layoutManager is not None else None
        if layoutNode is not None and layoutNode.GetNumberOfMaximizedViewNodes() > 0:
            viewNode = layoutNode.GetMaximizedViewNode(layoutNode.GetNumberOfMaximizedViewNodes() - 1)
            layoutManager.removeMaximizedViewNode(viewNode)
            return True
        if self.focusMode():
            self.setFocusMode(False)
            return True
        # Rung 4 (the active Draw tool) belongs to the Segment Editor's Esc shortcut
        if self.workspaces is not None and self.workspaces.transientModule and self.currentWorkspace() is None:
            self.closeTransientModule()
            return True
        return False

    @staticmethod
    def _hasKeyModifiers(event):
        """True if Shift, Ctrl, Alt or Meta is held (the keypad modifier does not count)."""
        return ui.hasKeyModifiers(event)

    def _onEscapeKey(self, event):
        """Esc reached the main window (the focus widget did not use it). Return True to consume it.

        The Esc ladder runs when Qt asks whether a shortcut may take the key (ShortcutOverride): accepting
        it there keeps the window-wide Esc shortcut of the Segment Editor from firing for the same press.
        The key press that follows is then consumed. Key presses without a shortcut override (sent
        directly to a widget) run the ladder too.

        A spin box or a closed combo box does not keep Esc: the focus moves to the module panel and the ladder
        runs in the same press, so that Esc leaves a transient module after the brush size was touched.
        """
        eventType = event.type()
        if eventType == qt.QEvent.ShortcutOverride:
            self._escapeHandled = False
        elif self._escapeHandled:
            self._escapeHandled = False
            return True
        if self._hasKeyModifiers(event):
            return False
        if slicer.app.activePopupWidget() is not None or slicer.app.activeModalWidget() is not None:
            return False
        focusWidget = slicer.app.focusWidget()
        if _keepsEscape(focusWidget):
            return False
        if _leavesFieldOnEscape(focusWidget):
            modulePanel = self._modulePanel()
            if modulePanel is not None and modulePanel.isVisible():
                modulePanel.setFocus(qt.Qt.OtherFocusReason)
            else:
                focusWidget.clearFocus()
        if not self.handleEscape():
            return False
        if eventType == qt.QEvent.ShortcutOverride:
            event.accept()
            self._escapeHandled = True
        return True

    def _onEvent(self, obj, event):
        """Event filter of the main window, its central widget and the inspector. Return True to consume the event."""
        eventType = event.type()
        if eventType in (qt.QEvent.ShortcutOverride, qt.QEvent.KeyPress, qt.QEvent.KeyRelease):
            if (eventType == qt.QEvent.KeyPress and event.key() in (qt.Qt.Key_PageUp, qt.Qt.Key_PageDown)
                    and (obj == self.mainWindow or obj == self._panelScrollArea) and self._forwardSliceKey(event)):
                return True
            if obj != self.mainWindow:
                return False
            key = event.key()
            if eventType != qt.QEvent.KeyRelease and key == qt.Qt.Key_Escape:
                self._cancelAltKey()
                return self._onEscapeKey(event)
            if self._altPending or key == qt.Qt.Key_Alt:
                self._onAltKey(event)
            return False
        if eventType in (qt.QEvent.MouseButtonPress, qt.QEvent.Wheel):
            # Alt+click and Alt+wheel are not a bare Alt press
            self._cancelAltKey()
            return False
        if eventType == qt.QEvent.Resize:
            if obj == self.mainWindow.centralWidget():
                if self._canvas is not None:
                    self._canvas.setGeometry(obj.rect)
                if self._toast is not None:
                    self._toast.updatePosition()
                if self._stickyToast is not None and self._stickyToast.isVisible():
                    self._stickyToast.updatePosition()
                if self._focusModePill is not None:
                    self._focusModePill.updatePosition()
                if self._inspectorHandle is not None:
                    self._inspectorHandle.updatePosition()
            elif obj == self._panelDock():
                self._onDockResized()
            elif obj == self.mainWindow:
                self._updateTopBarCompact()
                # In a small window the views keep their minimum width and the inspector gets narrower
                # than the stored width: give it back when the window grows
                self._schedulePanelWidthUpdate(shrink=False)
                # The views under the canvas follow the window again (the size they were held at is stale)
                self._releaseViewAreaSize()
                self._onMainWindowResized(event)
        elif eventType == qt.QEvent.Move and obj == self.mainWindow:
            position = (self.mainWindow.pos.x(), self.mainWindow.pos.y())
            if position != self._windowMovedTo:
                # The user (or the window manager) placed the window: it stays where it is
                self._windowGrownForMinimum = False
            self._windowMovedTo = None
        elif eventType in (qt.QEvent.ChildAdded, qt.QEvent.ChildPolished, qt.QEvent.LayoutRequest) and obj == self.mainWindow:
            if eventType == qt.QEvent.LayoutRequest and self._windowGrownForMinimum:
                # The minimum size may have become smaller (another layout): a window that Qt had to grow beyond the
                # screen comes back
                self._scheduleKeepWindowOnScreen()
            # Toolbars added later (extensions) are hidden like the others. A toolbar constructed with
            # the main window as parent is a child before it is a QToolBar, and it may be put in a
            # toolbar area (which requests a layout) only later: look for new toolbars when the event
            # loop runs again.
            if self._toolBarScanTimer is not None:
                self._toolBarScanTimer.start()
        elif eventType == qt.QEvent.LayoutRequest and obj == self._watchedPanelContents:
            # The module widget changed (a tool was chosen): the inspector may need to widen once
            self._schedulePanelWidthUpdate(shrink=False)
        elif eventType == qt.QEvent.Show and obj == self.mainWindow:
            if self._canvas is not None:
                # The canvas was set while the main window was hidden (no view widget was visible): cover now
                self._coverFocusPolicies()
        elif eventType == qt.QEvent.Show and obj == self._panelDock():
            # Shown again (Ctrl+\, Ctrl+5, hidden panels): back to the width of the workspaces
            self._schedulePanelWidthUpdate(shrink=True)
            self._updateInspectorHandle()
        elif eventType == qt.QEvent.Hide and obj == self._panelDock():
            self._updateInspectorHandle()
        return False

    def _forwardSliceKey(self, event):
        """PageUp or PageDown that would scroll the inspector, or that no widget used, step the slice view under the
        pointer, as the keyboard shortcuts say. Focus follows the mouse only when the pointer enters a view: after a
        scan is dropped or opened, the pointer is still over the view and the focus is on the tool tiles of Draw, so
        the keys scrolled the inspector instead. With the pointer over the inspector it scrolls as before; text
        inputs, popups and dialogs keep their keys, and so do chords (Ctrl+PageUp...). Return True if the key was given
        to a view.
        """
        if self._forwardingSliceKey:
            # (The view does not accept the key event: it comes back up through the main window while it is sent)
            return False
        if ui.hasKeyModifiers(event):
            return False
        if qt.QApplication.activePopupWidget() is not None or qt.QApplication.activeModalWidget() is not None:
            return False
        focusWidget = qt.QApplication.focusWidget()
        if _isTextInput(focusWidget) or _isInView(focusWidget):
            # A view that has the focus already stepped its slice (it does not accept the key event, so the event
            # comes up to the main window afterwards): forwarding it again would step twice, or step a second view
            return False
        hovered = qt.QApplication.widgetAt(qt.QCursor.pos())
        view = hovered
        while view is not None and not view.inherits("qMRMLSliceView"):
            view = view.parentWidget()
        if view is None or not hovered.isVisible():
            return False
        # The render widget handles the key (the widget under the pointer may be a child drawn over it)
        target = view.VTKWidget() if hasattr(view, "VTKWidget") else None
        if target is None:
            target = hovered
        target.setFocus(qt.Qt.MouseFocusReason)
        self._forwardingSliceKey = True
        try:
            qt.QApplication.sendEvent(target, qt.QKeyEvent(qt.QEvent.KeyPress, event.key(), event.modifiers(), event.text()))
        finally:
            self._forwardingSliceKey = False
        return True

    def _removeEventFilters(self):
        if self._eventFilter is None:
            return
        for obj in (self.mainWindow, self.mainWindow.centralWidget(), self._panelDock()):
            obj.removeEventFilter(self._eventFilter)
        if self._panelScrollArea is not None:
            try:
                self._panelScrollArea.removeEventFilter(self._eventFilter)
            except Exception:
                # Deleted with the module panel
                pass
            self._panelScrollArea = None
        if self._watchedPanelContents is not None:
            try:
                self._watchedPanelContents.removeEventFilter(self._eventFilter)
            except Exception:
                # Deleted with the module panel
                pass
            self._watchedPanelContents = None
        self._eventFilter = None

    #
    # Studio-only behaviours of the views, the save folder and the classic menus
    #

    @staticmethod
    def navigationBehaviours():
        """The Studio-only navigation behaviours from the settings: {leftDragWindowLevel, pageStepSlices, focusFollowsMouse}."""
        return {
            "leftDragWindowLevel": _settingsBool(*LEFT_DRAG_WINDOW_LEVEL_KEY),
            "pageStepSlices": max(0, _settingsInt(*PAGE_STEP_SLICES_KEY)),
            "focusFollowsMouse": _settingsBool(*FOCUS_FOLLOWS_MOUSE_KEY),
        }

    def _applyNavigationBehaviours(self, enabled):
        """Turn on the navigation behaviours of the views that Studio uses (USABILITY 9.1, SPEC 6.2), or (``enabled``
        False) restore the classic ones: left-drag window/level, PageUp/PageDown by ``Studio/PageStepSlices`` slices,
        clamped slice stepping, symmetric zoom and focus follows the mouse. They are application-wide statics of the
        view widgets and an application property (W3-02a): no setting that official Slicer reads is written. Builds without them
        are skipped.
        """
        behaviours = self.navigationBehaviours() if enabled else {"leftDragWindowLevel": False, "pageStepSlices": 0, "focusFollowsMouse": False}
        windowLevelWidget = getattr(slicer, "vtkMRMLWindowLevelWidget", None)
        if windowLevelWidget is not None and hasattr(windowLevelWidget, "SetLeftDragWindowLevel"):
            windowLevelWidget.SetLeftDragWindowLevel(behaviours["leftDragWindowLevel"])
        sliceIntersectionWidget = getattr(slicer, "vtkMRMLSliceIntersectionWidget", None)
        if sliceIntersectionWidget is not None:
            if hasattr(sliceIntersectionWidget, "SetPageStepSlices"):
                sliceIntersectionWidget.SetPageStepSlices(behaviours["pageStepSlices"])
            if hasattr(sliceIntersectionWidget, "SetClampSliceStepping"):
                sliceIntersectionWidget.SetClampSliceStepping(bool(enabled))
            if hasattr(sliceIntersectionWidget, "SetSymmetricZoom"):
                sliceIntersectionWidget.SetSymmetricZoom(bool(enabled))
        slicer.app.setProperty(FOCUS_FOLLOWS_MOUSE_PROPERTY, bool(behaviours["focusFollowsMouse"]))

    def _onSettingsDialogFinished(self, result=None):
        if self._active:
            self._applyNavigationBehaviours(True)

    @staticmethod
    def _isApplicationDirectory(path):
        """True if ``path`` is (in) a folder that never holds the user's files: the application, temporary or cache folders."""
        try:
            from . import draw

            directories = draw._applicationDirectories()
            return any(draw._isInside(path, directory) for directory in directories)
        except Exception:
            pass
        real = os.path.normcase(os.path.realpath(path))
        return any(real == os.path.normcase(os.path.realpath(directory)) for directory in (slicer.app.slicerHome, slicer.app.applicationDirPath()) if directory)

    def _sceneRootTarget(self):
        """The folder of the Save dialog when the scene root is not usable: the last save folder, else the home folder."""
        try:
            from . import draw

            lastFolder = str(slicer.util.settingsValue(draw.LAST_SAVE_FOLDER_KEY, "") or "")
        except Exception:
            lastFolder = str(slicer.util.settingsValue("Studio/LastSaveFolder", "") or "")
        if lastFolder and os.path.isdir(lastFolder):
            return lastFolder
        return qt.QDir.homePath()

    def _fixSceneRootDirectory(self):
        """The Save dialog (Ctrl+S) opens in the scene root directory: never let it be a missing folder (Qt then uses
        the current folder, the application folder) or an application folder (USABILITY 8, rule 6). No setting is written.
        """
        root = slicer.mrmlScene.GetRootDirectory()
        if root and os.path.isdir(root) and not self._isApplicationDirectory(root):
            return
        target = self._sceneRootTarget()
        if os.path.normcase(os.path.realpath(target)) == os.path.normcase(os.path.realpath(root or "")):
            return
        if self._studioSceneRoot is None:
            self._classicSceneRoot = root
        slicer.mrmlScene.SetRootDirectory(target)
        self._studioSceneRoot = slicer.mrmlScene.GetRootDirectory()

    def _restoreSceneRootDirectory(self):
        """Classic: put the scene root directory back if it is still the one Studio set."""
        if self._studioSceneRoot is not None and slicer.mrmlScene.GetRootDirectory() == self._studioSceneRoot:
            slicer.mrmlScene.SetRootDirectory(self._classicSceneRoot or "")
        self._studioSceneRoot = None
        self._classicSceneRoot = None

    def _onSceneEndClose(self, caller=None, event=None):
        if self._active:
            self._fixSceneRootDirectory()

    def _onSceneEndImport(self, caller=None, event=None):
        if self._active:
            self.workspaces.onSceneImported()

    def _onErrorLogVisibilityChanged(self, visible):
        """The error log opens at the bottom in Studio (tabbed with the Python console when that is there).

        Classic gets its own place back with the classic window state.
        """
        if visible and self._active:
            qt.QTimer.singleShot(0, self._dockErrorLogAtBottom)

    def _dockErrorLogAtBottom(self):
        mainWindow = self.mainWindow
        dock = mainWindow.errorLogDockWidget()
        if not self._active or dock is None or dock.isHidden():
            return
        if not dock.floating and mainWindow.dockWidgetArea(dock) == qt.Qt.BottomDockWidgetArea:
            return
        mainWindow.addDockWidget(qt.Qt.BottomDockWidgetArea, dock)
        python = mainWindow.findChild("QDockWidget", "PythonConsoleDockWidget")
        if python is not None and not python.isHidden() and not python.floating and mainWindow.dockWidgetArea(python) == qt.Qt.BottomDockWidgetArea:
            mainWindow.tabifyDockWidget(python, dock)
        dock.show()
        dock.raise_()

    #: Items of the shared menus that get a keyboard mnemonic in Studio, so that the ≡ menu is usable from the keyboard
    #: (F10, F, D: File > Add DICOM Data): (menu object name, module whose action it is, word that takes the mnemonic)
    _STUDIO_MNEMONICS = (("FileMenu", "dicom", "DICOM"),)

    def _addMenuMnemonics(self):
        """Give menu items without a mnemonic one in Studio (restored in Classic) when its letter is free in the menu.

        Also run when the menu is about to show: modules add their items when the application has started.
        """
        if not self._active:
            return
        for menuName, moduleName, word in self._STUDIO_MNEMONICS:
            menu = self.mainWindow.findChild("QMenu", menuName)
            module = getattr(slicer.modules, moduleName, None)
            action = module.action() if module is not None and hasattr(module, "action") else None
            if menu is None or action is None or action not in menu.actions():
                continue
            if any(action == handled for handled, text, studioText in self._mnemonicActions):
                continue
            text = action.text
            if "&" in text or word not in text:
                continue
            taken = {str(qt.QKeySequence.mnemonic(other.text).toString(qt.QKeySequence.PortableText)) for other in menu.actions() if other != action}
            if f"Alt+{word[0].upper()}" in taken:
                continue
            studioText = text.replace(word, "&" + word, 1)
            action.setText(studioText)
            self._mnemonicActions.append((action, text, studioText))

    def _removeMenuMnemonics(self):
        for action, text, studioText in self._mnemonicActions:
            try:
                if action.text == studioText:
                    action.setText(text)
            except Exception:
                # Deleted with its module
                pass
        self._mnemonicActions = []

    def _onViewMenuAboutToShow(self):
        """View menu in Studio: no status bar to show, and Module Panel says whether the inspector is shown."""
        if not self._active:
            return
        statusBarAction = self.mainWindow.findChild("QAction", "ShowStatusBarAction")
        if statusBarAction is not None:
            statusBarAction.setEnabled(False)
        dock = self._panelDock()
        toggle = dock.toggleViewAction()
        toggle.blockSignals(True)
        toggle.setChecked(not dock.isHidden())
        toggle.blockSignals(False)

    #
    # Cleanup
    #

    def dispose(self):
        """Restore Classic without remembering it, and remove every trace of the shell (tests)."""
        global _shell
        self.classic(persist=False)
        if self._classicSwitchAction is not None:
            self.mainWindow.removeAction(self._classicSwitchAction)
            viewMenu = self.mainWindow.findChild("QMenu", "ViewMenu")
            if viewMenu is not None:
                viewMenu.removeAction(self._classicSwitchAction)
                if self._classicSwitchSeparator is not None:
                    viewMenu.removeAction(self._classicSwitchSeparator)
                    self._classicSwitchSeparator = None
            self._classicSwitchAction.setObjectName("")
            self._classicSwitchAction.deleteLater()
            self._classicSwitchAction = None
        if _closeGuard is not None and _closeGuard.classicStateProvider == self._classicStateIfActive:
            _closeGuard.classicStateProvider = None
        if _shell is self:
            _shell = None

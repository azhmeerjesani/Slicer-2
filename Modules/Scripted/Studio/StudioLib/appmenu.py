"""App menu (≡) of the Slicer Studio top bar (SPEC 2.4).

It reuses the File, Edit, View and Help menus of the main window, and offers the workspaces,
Appearance (theme and density), common dialogs and the switch to the Classic interface.

Appearance settings are changed through the Appearance settings panel, so they are stored in
the fork's own keys (``Appearance/Theme``, ``Appearance/Density``) and never in keys that other
Slicer installations read (SPEC 9.7 rule 5). Only on builds without the theme setting (the Studio
module run by another Slicer), Dark and Light select the "Dark Slicer" and "Light Slicer" styles,
like the style combo box of the panel does.
"""

import qt
import slicer
from slicer.i18n import tr as _

from . import ui, workspaces

_THEMES = (("Dark", "moon"), ("Light", "sun"), ("System", "monitor"))
_DENSITIES = ("Comfortable", "Compact")
# Built-in styles whose palette does not follow the theme setting
_FIXED_PALETTE_STYLES = ("Dark Slicer", "Light Slicer")


def _menuText(text, shortcut=None):
    # The shortcut is displayed only (after a tab): the keys are handled by the Studio shortcuts,
    # a real shortcut on the menu action would make them ambiguous.
    return f"{text}\t{shortcut}" if shortcut else text


def stylesPanel():
    """The Appearance settings panel (qSlicerSettingsStylesPanel), or None."""
    dialog = slicer.app.settingsDialog()
    if dialog is None:
        return None
    return dialog.findChild("qSlicerSettingsStylesPanel")


def currentTheme():
    panel = stylesPanel()
    if panel is None:
        return None
    if panel.currentStyle in _FIXED_PALETTE_STYLES:
        return "Dark" if panel.currentStyle == "Dark Slicer" else "Light"
    return panel.theme if hasattr(panel, "theme") else None


def availableThemes():
    """Themes that ``setTheme()`` can apply: without the theme setting, the Dark and Light styles."""
    panel = stylesPanel()
    if panel is None:
        return ()
    return tuple(theme for theme, iconName in _THEMES) if hasattr(panel, "theme") else ("Dark", "Light")


def setTheme(theme):
    """Apply a theme ("Dark", "Light" or "System") like the Appearance settings panel does."""
    panel = stylesPanel()
    if panel is None:
        return False
    if hasattr(panel, "theme"):
        if panel.currentStyle in _FIXED_PALETTE_STYLES:
            # The user explicitly chose a theme here: switch back to the style that follows it
            panel.setCurrentStyle("Slicer")
        panel.setTheme(theme)
        return True
    # Builds without themes: use the matching built-in style
    if theme in ("Dark", "Light"):
        panel.setCurrentStyle(f"{theme} Slicer")
        return True
    return False


def currentDensity():
    panel = stylesPanel()
    if panel is None or not hasattr(panel, "density"):
        return None
    return panel.density


def setDensity(density):
    """Store the density ("Comfortable" or "Compact"). It takes effect after a restart."""
    panel = stylesPanel()
    if panel is None or not hasattr(panel, "density"):
        return False
    panel.setDensity(density)
    return True


class AppMenu:
    """The ≡ menu. ``menu`` is the QMenu."""

    def __init__(self, shell, parent):
        self.shell = shell
        mainWindow = shell.mainWindow
        self.menu = qt.QMenu(parent)
        self.menu.setObjectName("StudioAppMenu")
        self.menu.connect("aboutToShow()", self._update)

        self.workspaceActions = {}
        # Exclusive (drawn as radio items); none is checked while a transient module is shown
        self.workspaceGroup = qt.QActionGroup(self.menu)
        for index, ws in enumerate(workspaces.WORKSPACES):
            action = self.menu.addAction(_menuText(ws.title, f"Ctrl+Alt+{index + 1}"))
            action.setObjectName(f"Studio{ws.title}WorkspaceAction")
            action.setCheckable(True)
            self.workspaceGroup.addAction(action)
            action.setEnabled(workspaces.isAvailable(ws))
            action.connect("triggered()", lambda key=ws.key: self.shell.switchTo(key))
            self.workspaceActions[ws.key] = action
        allTools = self.menu.addAction(_menuText(_("All tools…"), "Ctrl+K"))
        allTools.setObjectName("StudioAllToolsAction")
        allTools.connect("triggered()", self.shell.openPalette)
        self.menu.addSeparator()

        # Existing main window menus (shared, not copied)
        for name in ("FileMenu", "EditMenu", "ViewMenu", "HelpMenu"):
            existingMenu = mainWindow.findChild("QMenu", name)
            if existingMenu is not None:
                self.menu.addMenu(existingMenu)
        self.menu.addSeparator()

        self.appearanceMenu = self.menu.addMenu(_("Appearance"))
        self.appearanceMenu.setObjectName("StudioAppearanceMenu")
        self.themeGroup = qt.QActionGroup(self.appearanceMenu)
        self.themeActions = {}
        for theme, iconName in _THEMES:
            action = self.appearanceMenu.addAction(_(theme))
            action.setObjectName(f"Studio{theme}ThemeAction")
            action.setCheckable(True)
            action.setProperty("studioIcon", iconName)
            self.themeGroup.addAction(action)
            action.connect("triggered()", lambda theme=theme: self._setTheme(theme))
            self.themeActions[theme] = action
        self.appearanceMenu.addSeparator()
        self.densityMenu = self.appearanceMenu.addMenu(_("Density"))
        self.densityMenu.setObjectName("StudioDensityMenu")
        self.densityGroup = qt.QActionGroup(self.densityMenu)
        self.densityActions = {}
        for density in _DENSITIES:
            action = self.densityMenu.addAction(_(density))
            action.setObjectName(f"Studio{density}DensityAction")
            action.setCheckable(True)
            self.densityGroup.addAction(action)
            action.connect("triggered()", lambda density=density: self._setDensity(density))
            self.densityActions[density] = action

        self.menu.addSeparator()
        self._addMainWindowAction(_("Settings…"), "Ctrl+2", mainWindow.findChild("QAction", "EditApplicationSettingsAction"), "settings")
        self._addMainWindowAction(_("Python console"), "Ctrl+3", self._pythonConsoleAction(), "terminal")
        self._addMainWindowAction(_("Error log"), "Ctrl+0", mainWindow.errorLogDockWidget().toggleViewAction(), "circle-alert")
        shortcuts = self.menu.addAction(_menuText(_("Keyboard shortcuts"), "Ctrl+/"))
        shortcuts.setObjectName("StudioShortcutsAction")
        shortcuts.setProperty("studioIcon", "keyboard")
        shortcuts.connect("triggered()", self.shell.toggleCheatSheet)
        self.menu.addSeparator()
        classic = self.menu.addAction(_menuText(_("Classic interface"), "Ctrl+Alt+0"))
        classic.setObjectName("StudioClassicInterfaceAction")
        classic.connect("triggered()", self.shell.classic)

        self.refreshIcons()
        ui.addPaletteListener(self.refreshIcons)

    def _pythonConsoleAction(self):
        console = self.shell.mainWindow.findChild("QDockWidget", "PythonConsoleDockWidget")
        return console.toggleViewAction() if console is not None else None

    def _addMainWindowAction(self, text, shortcut, mainWindowAction, iconName):
        action = self.menu.addAction(_menuText(text, shortcut))
        action.setProperty("studioIcon", iconName)
        if mainWindowAction is None:
            action.setEnabled(False)
        else:
            action.connect("triggered()", mainWindowAction.trigger)
        return action

    def refreshIcons(self):
        for action in self.menu.actions() + self.appearanceMenu.actions():
            iconName = action.property("studioIcon")
            if iconName:
                action.setIcon(ui.icon(iconName, 16))

    def _update(self):
        current = self.shell.currentWorkspace()
        for key, action in self.workspaceActions.items():
            action.setChecked(key == current)
        theme = currentTheme()
        themes = availableThemes()
        for name, action in self.themeActions.items():
            action.setEnabled(name in themes)
            action.setChecked(name == theme)
        density = currentDensity()
        for name, action in self.densityActions.items():
            action.setEnabled(density is not None)
            action.setChecked(name == density)

    def _setTheme(self, theme):
        setTheme(theme)

    def _setDensity(self, density):
        if currentDensity() == density:
            return
        if setDensity(density):
            self.shell.toast(_("Density: {density}. Takes effect after restart").format(density=_(density)))

    def dispose(self):
        ui.removePaletteListener(self.refreshIcons)

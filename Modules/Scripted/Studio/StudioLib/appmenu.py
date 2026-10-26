"""App menu (≡) of the Slicer Studio top bar (SPEC 2.4).

It reuses the File, Edit, View and Help menus of the main window (plug-ins add Studio items to Help with
``addHelpActions()``, removed when Studio is left), and offers the workspaces,
Search, the inspector and "Hide panels" toggles, Appearance (theme and density), common dialogs, "Check for
updates…" (A-08) and the switch to the Classic interface. While Studio is shown, View also has "Recent messages…" (the
last toast messages). The menu opens with F10 (and Alt, Alt+F/E/V/H on Windows and Linux, see the shell).

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


def _keyText(key):
    return ui.keyText(key)


def _focusModeKeyText():
    from . import shell

    return shell.focusModeKeyText()


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
        # One search, one name (USABILITY 4): the object name of the former "All tools…" item is kept
        search = self.menu.addAction(_menuText(_("Search…"), _keyText("Ctrl+K")))
        search.setObjectName("StudioAllToolsAction")
        search.setProperty("studioIcon", "search")
        search.connect("triggered()", self.shell.openPalette)
        self.searchAction = search
        self.menu.addSeparator()

        # Ways back to a hidden inspector and to hidden panels, with their keys
        self.inspectorAction = self.menu.addAction(_menuText(_("Show inspector"), shell.inspectorShortcutText()))
        self.inspectorAction.setObjectName("StudioInspectorAction")
        self.inspectorAction.setCheckable(True)
        self.inspectorAction.connect("triggered()", self.shell.toggleInspector)
        self.focusModeAction = self.menu.addAction(_menuText(_("Hide panels"), _focusModeKeyText()))
        self.focusModeAction.setObjectName("StudioFocusModeAction")
        self.focusModeAction.setCheckable(True)
        self.focusModeAction.connect("triggered()", self.shell.toggleFocusMode)
        self.menu.addSeparator()

        # Existing main window menus (shared, not copied)
        for name in ("FileMenu", "EditMenu", "ViewMenu", "HelpMenu"):
            existingMenu = mainWindow.findChild("QMenu", name)
            if existingMenu is not None:
                self.menu.addMenu(existingMenu)
        self.menu.addSeparator()
        # View > Recent messages… (removed from the shared View menu in Classic, see dispose())
        self._viewMenu = mainWindow.findChild("QMenu", "ViewMenu")
        self.recentMessagesAction = qt.QAction(_("Recent messages…"), mainWindow)
        self.recentMessagesAction.setObjectName("StudioRecentMessagesAction")
        self.recentMessagesAction.connect("triggered()", self.shell.showRecentMessages)
        self._recentMessagesSeparator = None
        if self._viewMenu is not None and hasattr(self.shell, "showRecentMessages"):
            self._recentMessagesSeparator = self._viewMenu.addSeparator()
            self._viewMenu.addAction(self.recentMessagesAction)
        # ≡ > Help items of the plug-ins (addHelpActions()), above the items of 3D Slicer
        self._helpMenu = mainWindow.findChild("QMenu", "HelpMenu")
        self._helpActions = []
        self._helpSeparator = None

        # The other theme in one step ("Light theme" while dark, "Dark theme" while light); the Appearance
        # submenu keeps the three themes (System included) and the density
        self.toggleThemeAction = self.menu.addAction(_("Light theme"))
        self.toggleThemeAction.setObjectName("StudioToggleThemeAction")
        self.toggleThemeAction.setProperty("studioIcon", "sun")
        self.toggleThemeAction.connect("triggered()", self.toggleTheme)
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
        # In-app updates (A-08): "Update to X…" when an update is known, else "Check for updates…"
        self.updatesAction = self.menu.addAction(_("Check for updates…"))
        self.updatesAction.setObjectName("StudioAppMenuUpdatesAction")
        self.updatesAction.setProperty("studioIcon", "download")
        self.updatesAction.connect("triggered()", self._onUpdatesTriggered)
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

    def addHelpActions(self, actions):
        """Show ``actions`` (QActions of a plug-in, for example the guided tours) at the top of ≡ > Help while Studio is shown.

        The Help menu is the one of the main window, shared with Classic: the actions are removed by
        ``removeHelpActions()`` and when Studio is left (``dispose()``).
        """
        if self._helpMenu is None:
            return
        if self._helpSeparator is None:
            existing = self._helpMenu.actions()
            self._helpSeparator = qt.QAction(self._helpMenu)
            self._helpSeparator.setSeparator(True)
            self._helpSeparator.setObjectName("StudioHelpSeparator")
            if existing:
                self._helpMenu.insertAction(existing[0], self._helpSeparator)
            else:
                self._helpMenu.addAction(self._helpSeparator)
        for action in actions:
            if action in self._helpActions:
                continue
            self._helpMenu.insertAction(self._helpSeparator, action)
            self._helpActions.append(action)

    def removeHelpActions(self, actions):
        """Remove plug-in actions added by ``addHelpActions()`` from ≡ > Help."""
        if self._helpMenu is None:
            return
        for action in list(actions):
            if action in self._helpActions:
                self._helpMenu.removeAction(action)
                self._helpActions.remove(action)
        if not self._helpActions and self._helpSeparator is not None:
            self._helpMenu.removeAction(self._helpSeparator)
            self._helpSeparator.deleteLater()
            self._helpSeparator = None

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
        # Also when the palette changed: the theme item names the other theme
        self._updateToggleThemeAction()
        for action in self.menu.actions() + self.appearanceMenu.actions():
            iconName = action.property("studioIcon")
            if iconName:
                action.setIcon(ui.icon(iconName, 16))

    def _update(self):
        current = self.shell.currentWorkspace()
        for key, action in self.workspaceActions.items():
            action.setChecked(key == current)
        self.inspectorAction.setChecked(self.shell.isInspectorVisible())
        self.focusModeAction.setChecked(self.shell.focusMode())
        theme = currentTheme()
        themes = availableThemes()
        for name, action in self.themeActions.items():
            action.setEnabled(name in themes)
            action.setChecked(name == theme)
        self._updateToggleThemeAction(themes)
        density = currentDensity()
        for name, action in self.densityActions.items():
            action.setEnabled(density is not None)
            action.setChecked(name == density)
        self._updateUpdatesAction()

    @staticmethod
    def _updatesModule():
        import sys

        # Only once the update service runs (imported by the Studio module at startup)
        return sys.modules.get("StudioLib.updates")

    def _updateUpdatesAction(self):
        updates = self._updatesModule()
        available = updates.availableUpdate() if updates is not None else None
        self.updatesAction.setText(_("Update to {version}…").format(version=available) if available else _("Check for updates…"))
        self.updatesAction.setEnabled(updates is not None and updates.instance() is not None)

    def _onUpdatesTriggered(self):
        updates = self._updatesModule()
        if updates is None or updates.instance() is None:
            return
        if updates.availableUpdate():
            updates.updateNow()
        else:
            updates.showUpdates(check=True)

    def _setTheme(self, theme):
        setTheme(theme)

    def otherTheme(self):
        """The theme that ``toggleTheme()`` applies: "Light" while the application is dark (also with System), else "Dark"."""
        return "Light" if ui.isDark() else "Dark"

    def _updateToggleThemeAction(self, themes=None):
        themes = availableThemes() if themes is None else themes
        other = self.otherTheme()
        self.toggleThemeAction.setText(_("Light theme") if other == "Light" else _("Dark theme"))
        iconName = "sun" if other == "Light" else "moon"
        if self.toggleThemeAction.property("studioIcon") != iconName:
            self.toggleThemeAction.setProperty("studioIcon", iconName)
            self.toggleThemeAction.setIcon(ui.icon(iconName, 16))
        self.toggleThemeAction.setEnabled(other in themes)

    def toggleTheme(self):
        """Switch to the other theme (≡ > Light theme / Dark theme)."""
        theme = self.otherTheme()
        if theme in availableThemes():
            setTheme(theme)

    def _setDensity(self, density):
        if currentDensity() == density:
            return
        if setDensity(density):
            self.shell.toast(_("Density: {density}. Takes effect after restart").format(density=_(density)))

    def dispose(self):
        ui.removePaletteListener(self.refreshIcons)
        self.removeHelpActions(self._helpActions)
        if self._viewMenu is not None:
            self._viewMenu.removeAction(self.recentMessagesAction)
            if self._recentMessagesSeparator is not None:
                self._viewMenu.removeAction(self._recentMessagesSeparator)
        self._recentMessagesSeparator = None
        self.recentMessagesAction.setObjectName("")
        self.recentMessagesAction.deleteLater()

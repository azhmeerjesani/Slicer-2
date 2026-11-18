"""The "Profile" panel of the application settings: where the user profile is, what it holds, and what a previous
build left behind (``studio_profile``).

- The profile folder, with Show and Change… (a folder chosen here is used from the next start: ``studio_profile``
  copies the profile there when that folder is empty; the old folder is kept). The environment variable
  ``SLICER_STUDIO_PROFILE_DIR`` wins over the setting: the panel says so.
- The size of each part (autosave, extensions, Python packages, update state, copies of older builds), measured in a
  background thread when the panel is first shown.
- What was carried forward from previous builds (``StudioProfile.json``).
- Extensions and Python packages of a previous build that could not be carried forward (another Python or extensions
  ABI, not enough space, or a build that kept them inside the application): Reinstall… installs them again.

It writes only the fork-only keys ``Studio/Profile/*``. Not added under ``--testing``.
"""

import os
import sys
import threading

import ctk
import qt
import slicer
from slicer.i18n import tr as _

PANEL_OBJECT_NAME = "StudioProfileSettingsPanel"
PANEL_NAME = "Profile"


def formatBytes(value):
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if value >= size:
            return f"{value / size:.1f} {unit}"
    return f"{value} B"


def parts(profile):
    """``(label, folder)`` of each part of the profile."""
    import studio_profile

    return [
        (_("Autosave"), os.path.join(profile, "StudioAutosave")),
        (_("Extensions"), os.path.join(profile, studio_profile.EXTENSIONS_FOLDER)),
        (_("Removed extensions (kept 30 days)"), os.path.join(profile, studio_profile.TRASH_FOLDER)),
        (_("Python packages"), os.path.join(profile, studio_profile.PYTHON_FOLDER)),
        (_("Update state"), os.path.join(profile, "StudioUpdate")),
        (_("Copies from older builds"), os.path.join(profile, studio_profile.LEGACY_FOLDER)),
    ]


def leftBehind(profile):
    """``(extensions, packages)``: names that a previous build had and this one does not."""
    import studio_profile
    from . import profile_legacy

    extensions, packages = set(), set()
    for _folder, data in profile_legacy.snapshots(profile):
        extensions.update(str(name) for name in data.get("extensions") or [])
        packages.update(str(entry.get("name")) for entry in data.get("packages") or [] if isinstance(entry, dict) and entry.get("name"))
    current = studio_profile.pythonUserBase(profile)
    for _folder, names in studio_profile.previousPythonFolders(profile, current):
        packages.update(names)
    model = slicer.app.extensionsManagerModel()
    if model is not None:
        installed = set(model.installedExtensions)
        extensions = {name for name in extensions if name not in installed}
    try:
        import importlib.metadata

        present = {dist.metadata["Name"].lower().replace("_", "-") for dist in importlib.metadata.distributions() if dist.metadata["Name"]}
    except Exception:
        present = set()
    packages = {name for name in packages if name.lower().replace("_", "-") not in present}
    return sorted(extensions, key=str.lower), sorted(packages, key=str.lower)


class ProfileSettingsPanel(ctk.ctkSettingsPanel):
    def __init__(self, *args, **kwargs):
        ctk.ctkSettingsPanel.__init__(self, *args, **kwargs)
        self.setObjectName(PANEL_OBJECT_NAME)
        self._built = False
        self._sizes = None
        self._sizesTimer = None

    def showEvent(self, event):
        if not self._built:
            self._built = True
            self._build()
        self.refresh()

    def _build(self):
        layout = qt.QVBoxLayout(self)
        intro = qt.QLabel(_("Your settings, extensions, Python packages, autosave and update state are kept in this folder, "
                            "outside the application: reinstalling or rebuilding Slicer Studio keeps them."))
        intro.wordWrap = True
        layout.addWidget(intro)
        folderRow = qt.QHBoxLayout()
        self.folderLabel = qt.QLabel()
        self.folderLabel.setObjectName("StudioProfileFolderLabel")
        self.folderLabel.setTextInteractionFlags(qt.Qt.TextSelectableByMouse)
        self.folderLabel.wordWrap = True
        folderRow.addWidget(self.folderLabel, 1)
        self.showButton = qt.QPushButton(_("Show"))
        self.showButton.connect("clicked()", self.showFolder)
        folderRow.addWidget(self.showButton)
        self.changeButton = qt.QPushButton(_("Change…"))
        self.changeButton.connect("clicked()", self.changeFolder)
        folderRow.addWidget(self.changeButton)
        layout.addLayout(folderRow)
        self.sourceLabel = qt.QLabel()
        self.sourceLabel.wordWrap = True
        layout.addWidget(self.sourceLabel)
        self.sizesLabel = qt.QLabel()
        self.sizesLabel.wordWrap = True
        layout.addWidget(self.sizesLabel)
        self.historyLabel = qt.QLabel()
        self.historyLabel.wordWrap = True
        layout.addWidget(self.historyLabel)
        self.leftBehindLabel = qt.QLabel()
        self.leftBehindLabel.wordWrap = True
        layout.addWidget(self.leftBehindLabel)
        self.reinstallButton = qt.QPushButton(_("Reinstall…"))
        self.reinstallButton.connect("clicked()", self.reinstall)
        layout.addWidget(self.reinstallButton)
        layout.addStretch(1)

    def refresh(self):
        import studio_profile

        profile = studio_profile.root()
        self.folderLabel.text = profile or _("(none)")
        source, problem = studio_profile.rootSource()
        pending = slicer.app.userSettings().value(studio_profile.PENDING_DIRECTORY_KEY)
        lines = []
        if source == "environment":
            lines.append(_("Set by the environment variable {name}: Change… has no effect while it is set.").format(name=studio_profile.ENVIRONMENT_VARIABLE))
            self.changeButton.enabled = False
        elif source == "setting":
            lines.append(_("Chosen in these settings."))
        else:
            lines.append(_("The default folder."))
        if problem:
            lines.append(problem)
        if pending:
            lines.append(_("From the next start: {folder}").format(folder=pending))
        self.sourceLabel.text = "\n".join(lines)
        manifest = studio_profile.readJson(os.path.join(profile, studio_profile.MANIFEST_FILE)) if profile else None
        history = []
        for _identifier, entry in sorted(((manifest or {}).get("migrations") or {}).items(), key=lambda item: str(item[1].get("at")), reverse=True)[:8]:
            history.append(f"{entry.get('at', '')[:10]} · {entry.get('kind', '')} · {entry.get('sourceName', '')}")
        self.historyLabel.text = (_("Carried forward:") + "\n" + "\n".join(history)) if history else ""
        self._extensionsLeft, self._packagesLeft = leftBehind(profile) if profile else ([], [])
        text = []
        if self._extensionsLeft:
            text.append(_("Extensions of a previous build that are not installed: {names}").format(names=", ".join(self._extensionsLeft)))
        if self._packagesLeft:
            text.append(_("Python packages of a previous build that are not installed: {names}").format(names=", ".join(self._packagesLeft)))
        self.leftBehindLabel.text = "\n".join(text)
        self.reinstallButton.visible = bool(text)
        self._measure(profile)

    def _measure(self, profile):
        if not profile or self._sizesTimer is not None:
            return
        self.sizesLabel.text = _("Measuring…")
        result = {}

        def run():
            import studio_profile

            result["sizes"] = [(label, studio_profile.treeSize(folder) if os.path.isdir(folder) else 0) for label, folder in parts(profile)]

        thread = threading.Thread(target=run, name="StudioProfileSizes", daemon=True)
        thread.start()
        timer = qt.QTimer()
        timer.setInterval(200)

        def poll():
            if thread.is_alive():
                return
            timer.stop()
            self._sizesTimer = None
            sizes = result.get("sizes") or []
            self.sizesLabel.text = "\n".join(f"{label}: {formatBytes(size)}" for label, size in sizes if size)

        timer.connect("timeout()", poll)
        self._sizesTimer = timer
        timer.start()

    def showFolder(self):
        import studio_profile

        profile = studio_profile.root()
        if profile:
            qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(profile))

    def changeFolder(self):
        import studio_profile

        folder = qt.QFileDialog.getExistingDirectory(self, _("Profile folder"), studio_profile.root())
        if not folder:
            return
        forbidden = studio_profile.applicationRoots(str(slicer.app.slicerHome), sys.platform)
        problem = studio_profile.validateOverride(folder, forbidden)
        current = studio_profile.root()
        if problem is None and current and os.path.realpath(folder) != os.path.realpath(current) and (
                studio_profile.isInside(folder, current) or studio_profile.isInside(current, folder)):
            problem = _("it is inside the current profile folder (or holds it)")
        if problem is not None:
            slicer.util.warningDisplay(_("This folder cannot hold the profile: {reason}.").format(reason=problem), windowTitle=_("Profile folder"))
            return
        settings = slicer.app.userSettings()
        settings.setValue(studio_profile.PENDING_DIRECTORY_KEY, folder)
        settings.sync()
        if slicer.util.confirmYesNoDisplay(_("The profile moves to {folder} when Slicer Studio starts again: it is copied there if that "
                                             "folder is empty (the current folder is kept). Close other Slicer Studio windows first.\n\n"
                                             "Restart now?").format(folder=folder), windowTitle=_("Profile folder")):
            slicer.util.restart()
        self.refresh()

    def reinstall(self):
        extensions, packages = self._extensionsLeft, self._packagesLeft
        if not extensions and not packages:
            return
        names = extensions + packages
        if not slicer.util.confirmOkCancelDisplay(_("Install these again from the internet?\n\n{names}").format(names=", ".join(names)),
                                                  windowTitle=_("Reinstall")):
            return
        failed = []
        model = slicer.app.extensionsManagerModel()
        with slicer.util.tryWithErrorDisplay(_("Reinstalling failed."), waitCursor=True):
            for name in extensions:
                if model is None or not model.installExtensionFromServer(name, False):
                    failed.append(name)
            if packages:
                slicer.util.pip_install(packages)
        if failed:
            slicer.util.warningDisplay(_("These extensions could not be installed: {names}").format(names=", ".join(failed)), windowTitle=_("Reinstall"))
        if extensions:
            slicer.util.infoDisplay(_("Restart Slicer Studio to use the extensions."), windowTitle=_("Reinstall"))
        self.refresh()


_panel = None


def install():
    """Add the Profile panel to the application settings (idempotent; never under --testing)."""
    global _panel
    if slicer.app.testingEnabled():
        return None
    try:
        import studio_profile  # noqa: F401
    except ImportError:
        return None  # a build without the user profile
    dialog = slicer.app.settingsDialog()
    if dialog is None:
        return None
    existing = dialog.findChild(qt.QWidget, PANEL_OBJECT_NAME)
    if existing is not None:
        return existing
    _panel = ProfileSettingsPanel()
    dialog.addPanel(_(PANEL_NAME), _panel)
    return _panel


def restartNoticeNeeded():
    import studio_profile

    value = slicer.app.revisionUserSettings().value(studio_profile.RESTART_KEY)
    return str(value).lower() in ("1", "true")


def showRestartNoticeOnce():
    """After the profile carried extensions forward in this start, their libraries are known to the application only
    from the next start (the launcher set them before): offer a restart, once."""
    try:
        import studio_profile
    except ImportError:
        return False
    if slicer.app.testingEnabled() or not restartNoticeNeeded():
        return False
    settings = slicer.app.revisionUserSettings()
    settings.remove(studio_profile.RESTART_KEY)
    settings.sync()

    def ask():
        if slicer.util.confirmYesNoDisplay(_("Your extensions from the previous version of Slicer Studio were brought into this one. "
                                             "Some of them work only after Slicer Studio starts again.\n\nRestart now?"),
                                           windowTitle=_("Slicer Studio")):
            slicer.util.restart()

    qt.QTimer.singleShot(3000, ask)
    return True

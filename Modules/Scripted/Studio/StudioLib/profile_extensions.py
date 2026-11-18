"""macOS: copy the extensions that the user installs, updates or uninstalls into the user profile right away.

On macOS the application installs extensions inside the bundle (``Contents/Extensions-<rev>``), so dragging a new
build over the old one would lose them. ``studio_profile`` keeps a mirror in the profile (the store) and syncs it at
every start; this module syncs it as soon as the Extensions Manager installs, updates or uninstalls an extension, so that
a reinstall right after (without a restart in between) keeps it too. An uninstalled extension goes to the trash of the
profile (``StudioExtensions-trash``), never deleted at once.

Linux needs nothing here: the extensions are installed in the store itself (``Extensions/InstallPath``).
"""

import logging
import os
import sys
import threading

import qt
import slicer

#: Wait for the Extensions Manager to finish a batch (dependencies are installed one after the other)
SYNC_DELAY_MS = 3000

_state = {"connected": False, "timer": None, "running": False, "again": False}


def locations():
    """``(store, bundle, trash)`` of this application, or None where there is nothing to sync."""
    if sys.platform != "darwin":
        return None
    try:
        import studio_profile
    except ImportError:
        return None
    app = slicer.app
    if not app.isInstalled or app.testingEnabled() or studio_profile.settingsDisabled(app):
        return None
    if studio_profile.portableBuild() or studio_profile.rootSource()[1]:
        return None  # a portable build, or the chosen profile folder is missing (this run uses the default)
    serverRevision = str(studio_profile.buildConfig().get("EXTENSIONS_SERVER_REVISION") or "")
    profile = studio_profile.root()
    if not serverRevision or not profile:
        return None
    store = studio_profile.extensionsStore(profile, serverRevision, str(app.os), str(app.arch))
    bundle = str(app.extensionsInstallPath)
    return store, bundle, os.path.join(profile, studio_profile.TRASH_FOLDER)


def install():
    """Connect to the Extensions Manager (idempotent). Return True when connected."""
    if _state["connected"]:
        return True
    if locations() is None:
        return False
    model = slicer.app.extensionsManagerModel()
    if model is None:
        return False
    for signal in (model.extensionInstalled, model.extensionUpdated, model.extensionUninstalled):
        signal.connect(lambda name: schedule())
    _state["connected"] = True
    return True


def schedule():
    timer = _state["timer"]
    if timer is None:
        timer = qt.QTimer()
        timer.setSingleShot(True)
        timer.setInterval(SYNC_DELAY_MS)
        timer.connect("timeout()", _start)
        _state["timer"] = timer
    timer.start()


_lock = threading.Lock()


def _start():
    where = locations()
    if where is None:
        return
    with _lock:
        if _state["running"]:
            _state["again"] = True
            return
        _state["running"] = True
    threading.Thread(target=_run, args=where, name="StudioProfileExtensions", daemon=True).start()


def _run(store, bundle, trash):
    import studio_profile

    while True:
        try:
            with studio_profile.ProfileLock(os.path.dirname(os.path.dirname(store)), timeout=30) as lock:
                if lock.acquired:
                    plan = studio_profile.syncBundle(store, bundle, trash)
                    if plan["problems"]:
                        logging.warning("Slicer Studio could not keep a copy of these extensions in the user profile: "
                                        + ", ".join(f"{name} ({reason})" for name, reason in plan["problems"].items()))
        except Exception as error:
            logging.warning(f"Slicer Studio could not keep a copy of the extensions in the user profile: {error}")
        with _lock:
            if not _state["again"]:
                _state["running"] = False
                return
            _state["again"] = False

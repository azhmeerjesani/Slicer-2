r"""Guided tours of Slicer Studio and the 'Where did it go?' map (T-01), a Studio plug-in (``install(shell)``).

Like the "Take a tour" of photo editors, a tour dims the window, spotlights one real control at a time and explains
it in a card next to it, with how the same thing is done in official 3D Slicer. The content is data
(``StudioLib.tours``, ``StudioLib.migration``); this module plays it:

- ``TourPlayer``: the overlay (a child of the main window, so it behaves the same on every platform), the
  spotlight with its accent ring, the card (title, text, an "In 3D Slicer" line, keys, step counter, progress dots,
  Back, Next and Skip tour) placed beside the control without covering it, never off the window. Right, Enter and
  Space go on, Left goes back, Esc leaves (the overlay is the top rung of the shell's Esc ladder), Tab stays in the
  card and clicks on the dimmed area do nothing. Steps whose control is missing or hidden are skipped.
- A tour may show another workspace or the inspector. When it ends, in any way, it restores the workspace (or the
  module opened from Search), the layout, the inspector and the Studio settings that the visit changed, and removes
  the empty segmentation that the Segment Editor creates when it is first shown on an empty scene: the scene and the
  settings are as before, nothing is downloaded.
- ``WhereMap``: the non-modal, searchable 'Where did it go?' panel over the inspector; "Show me" spotlights the
  control, "Open Classic" switches to the Classic interface for what Studio deliberately hides.
- Entry points: a first-start offer (a card on the Library canvas, or a notice line in the inspector, never modal,
  shown once: ``Studio/Tour/Offered``), ≡ > Help (Take a tour…, Tour this workspace…, Coming from 3D Slicer…,
  Where did it go?) and a "Tour this workspace" link in the help popover of the panel header.

API for other work packages (the command palette): ``startTour(tourId)``, ``startWorkspaceTour()``, ``stopTour()``,
``activeTour()``, ``availableTours()``, ``showMigrationMap(query)``, ``showMe(rowId)`` and ``migration.lookup(text)``.

Nothing runs under ``--testing`` or ``--no-main-window`` or in Classic (the shell does not exist then; the first-start
offer is never scheduled under ``--testing``), and nothing starts while a modal dialog is open. Settings: only
``Studio/Tour/Offered`` and ``Studio/Tour/Completed`` (completed tours as "<id>@<content version>").
"""

import contextlib
import logging
import time
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import ui

OFFERED_KEY = "Studio/Tour/Offered"
COMPLETED_KEY = "Studio/Tour/Completed"
#: Settings group of the tours: never restored by a tour
_TOUR_GROUP = "Studio/Tour/"
#: Studio settings of the views that a tour shows and puts back (the last workspace; each workspace's layout is
#: added from workspaces.WORKSPACES). With the keys that the tour's own changes wrote, they are the only settings
#: that a tour restores: a setting the user changes during a tour is kept.
_VIEW_KEYS = ("Studio/LastWorkspace",)

#: Spotlight: padding around the control, corner radius and accent ring width
SPOTLIGHT_PADDING = 6
SPOTLIGHT_RADIUS = 8
RING_WIDTH = 2
#: Opacity of the dimmed area in the dark and the light theme
DIM_ALPHA_DARK = 0.62
DIM_ALPHA_LIGHT = 0.42
#: Card: width (at a 13 px body), padding, corner radius, pointer size and distance to the spotlight
CARD_WIDTH = 372
CARD_PADDING = 20
CARD_RADIUS = 12
POINTER_SIZE = 9
CARD_GAP = 6
#: Margin kept between the card and the edges of the window
WINDOW_MARGIN = 10
#: How often the spotlight follows its control (resizes, docks, layouts) and checks a "Try it" step, in ms
FOLLOW_INTERVAL_MS = 120
#: First-start offer: delay after Studio is applied, retry interval while another prompt is shown, maximum retries
OFFER_DELAY_MS = 1500
OFFER_RETRY_MS = 1000
OFFER_MAX_RETRIES = 120
OFFER_CARD_WIDTH = 320
#: 'Where did it go?' panel
MAP_PANEL_WIDTH = 420
MAP_PANEL_TOP = 52
MAP_PANEL_MARGIN = 12
#: Segmentations created by the Segment Editor while a tour shows Draw are removed if they stayed empty
_GUARDED_NODE_CLASSES = ("vtkMRMLSegmentationNode",)

#: Two keys shown as a range (1 to 0, Alt+1 to Alt+4) rather than as alternatives
_KEY_RANGES = (("1", "0"), ("Alt+1", "Alt+4"))

_plugin = None


def _tours():
    from . import tours

    return tours


def _migration():
    from . import migration

    return migration


def _shellModule():
    from . import shell

    return shell


def _isAlive(widget):
    if widget is None:
        return False
    try:
        widget.objectName
    except ValueError:
        return False
    return True


def _refuseDrop(event):
    """Take a drag or drop event without dropping anything (it does not reach the main window, which loads files)."""
    event.setDropAction(qt.Qt.IgnoreAction)
    event.accept()


def _sameValue(first, second):
    try:
        return bool(first == second)
    except Exception:
        return False


def _settingsBool(key, default=False):
    return slicer.util.settingsValue(key, default, converter=slicer.util.toBool)


def _modalOpen():
    return slicer.app.activeModalWidget() is not None


def _font(widget, basePx, weight=None, letterSpacing=None):
    ui._font(widget, ui.fontPx(basePx), weight, letterSpacing)


def _label(text, objectName, basePx=13, weight=None, wrap=True):
    label = qt.QLabel(text)
    label.setObjectName(objectName)
    label.setWordWrap(wrap)
    label.setTextFormat(qt.Qt.PlainText)
    _font(label, basePx, weight)
    return label


def completedTours():
    """Completed tours as stored ("<id>@<content version>")."""
    value = qt.QSettings().value(COMPLETED_KEY)
    if not value:
        return []
    if isinstance(value, str):
        return [part for part in (piece.strip() for piece in value.split(",")) if part]
    return [str(item) for item in value]


def _markCompleted(tourId):
    entry = f"{tourId}@{_tours().CONTENT_VERSION}"
    entries = completedTours()
    if entry not in entries:
        entries = [item for item in entries if not item.startswith(f"{tourId}@")] + [entry]
        qt.QSettings().setValue(COMPLETED_KEY, entries)


def isOffered():
    return _settingsBool(OFFERED_KEY, False)


def _markOffered():
    if not isOffered():
        qt.QSettings().setValue(OFFERED_KEY, True)


#
# Targets
#


def _scrollIntoView(widget):
    """Scroll the nearest scroll area that holds ``widget`` so that the widget is shown (its top, when it is taller)."""
    if widget is None or not _isAlive(widget):
        return
    ancestor = widget.parentWidget()
    while ancestor is not None:
        if ancestor.inherits("QScrollArea") and ancestor.widget() is not None and ancestor.widget().isAncestorOf(widget):
            try:
                ancestor.ensureWidgetVisible(widget, 0, 16)
            except Exception:
                logging.error(f"Studio tour: scrolling to the control failed:\n{traceback.format_exc()}")
            return
        ancestor = ancestor.parentWidget()


def resolveTarget(target, shell):
    """The visible widget of a step target (objectName path, function, or a tuple of alternatives), or None."""
    alternatives = target if isinstance(target, (tuple, list)) else (target,)
    for alternative in alternatives:
        widget = None
        if callable(alternative):
            try:
                widget = alternative(shell)
            except Exception:
                logging.error(f"Studio tour: target function failed:\n{traceback.format_exc()}")
                widget = None
        else:
            widget = _migration().resolveTarget(alternative, shell.mainWindow)
        if widget is None or not _isAlive(widget) or not widget.isWidgetType():
            continue
        if not _isShown(widget, shell.mainWindow) and widget.isVisible():
            # A control that exists but is scrolled out of its page (the Library below the fold in a small window):
            # scroll it into view instead of skipping the step
            _scrollIntoView(widget)
        if _isShown(widget, shell.mainWindow):
            return widget
    return None


def _isShown(widget, mainWindow):
    """True if ``widget`` is visible, not empty and at least partly inside the main window."""
    if not widget.isVisible() or widget.width < 4 or widget.height < 4:
        return False
    # Not obscured by other widgets (the Library canvas over the views); a container is shown through its children
    if widget.visibleRegion().isEmpty() and not any(
            child.isVisible() and not child.visibleRegion().isEmpty() for child in widget.findChildren("QWidget")):
        return False
    rect = _rectIn(widget, mainWindow)
    return rect.intersects(mainWindow.rect)


def _rectIn(widget, mainWindow):
    topLeft = widget.mapTo(mainWindow, qt.QPoint(0, 0))
    return qt.QRect(topLeft.x(), topLeft.y(), widget.width, widget.height)


def _pageContentItems(column, canvas):
    """``[(rect, interactive)]`` of what the visible part of the Library page ``column`` paints, in ``canvas``
    coordinates: its labels (the extent of their text), buttons, fields and other leaf widgets, and its cards and dashed
    drop zone. ``interactive`` is True for buttons and fields. Containers and spacers, which span the column, are not
    content.
    """
    visible = column.visibleRegion().boundingRect()
    if visible.isEmpty():
        return []
    visible = qt.QRect(column.mapTo(canvas, visible.topLeft()), visible.size())
    items = []
    for widget in column.findChildren("QWidget"):
        if not widget.isVisible() or widget.isWindow() or widget.width <= 0 or widget.height <= 0:
            continue
        rect = _rectIn(widget, canvas)
        if widget.inherits("QLabel"):
            # The text, not the label box: a caption is as wide as the column
            textWidth = min(widget.width, widget.sizeHint.width())
            try:
                alignment = int(widget.property("alignment"))
            except (TypeError, ValueError):
                alignment = int(qt.Qt.AlignLeft)
            if alignment & int(qt.Qt.AlignRight):
                rect.setLeft(rect.right() - textWidth + 1)
            elif alignment & int(qt.Qt.AlignHCenter):
                rect.setLeft(rect.left() + (widget.width - textWidth) // 2)
            rect.setWidth(textWidth)
        elif not (widget.property("slicerRole") or widget.objectName == "StudioDropZoneFrame" or str(widget.objectName).endswith("Card")):
            container = any(child.isWidgetType() and child.isVisible() for child in widget.children())
            if container or widget.className() == "QWidget":
                continue
        rect = rect.intersected(visible)
        if not rect.isEmpty():
            interactive = any(widget.inherits(className) for className in ("QAbstractButton", "QLineEdit", "QComboBox", "QAbstractSpinBox"))
            items.append((rect, interactive))
    return items


def _pageContentRects(column, canvas):
    """The rectangles of ``_pageContentItems()``."""
    return [rect for rect, _interactive in _pageContentItems(column, canvas)]


def _existsAnywhere(target, shell):
    """True if a widget of the target exists in the main window, shown or not."""
    alternatives = target if isinstance(target, (tuple, list)) else (target,)
    for alternative in alternatives:
        if callable(alternative):
            return True
        name = str(alternative).split("/")[-1]
        if shell.mainWindow.findChild("QWidget", name) is not None:
            return True
    return False


def _stepMayShow(step, shell):
    """Whether a step will probably be shown (for the step counter): its feature is there and its control exists or
    appears with its workspace.
    """
    if step.when is not None:
        try:
            if not step.when(shell):
                return False
        except Exception:
            logging.error(f"Studio tour: condition of step {step.id} failed:\n{traceback.format_exc()}")
            return False
    if step.workspace is not None:
        from . import workspaces

        ws = workspaces.workspace(step.workspace)
        if ws is None or not workspaces.isAvailable(ws):
            return False
        if shell.currentWorkspace() != ws.key:
            return True
    return _existsAnywhere(step.target, shell)


#
# What a tour changes, and how it is put back
#


class _ViewState:
    """The user's workspace, module, layout, inspector, hidden panels, keyboard focus and scene nodes, captured when a
    tour starts and restored when it ends.

    Settings: only the Studio/* keys that the tour's own changes wrote (see ``tracking()``) and the view keys
    (``_viewKeys()``) are put back. Anything the user sets meanwhile (a shortcut still works during a tour) is kept.
    """

    def __init__(self, shell):
        self.shell = shell
        self.workspace = shell.currentWorkspace()
        self.transientModule = shell.workspaces.transientModule if shell.workspaces is not None else None
        self.transientOrigin = shell.transientOrigin()
        layoutManager = slicer.app.layoutManager()
        self.layout = layoutManager.layout if layoutManager is not None else None
        self.inspectorVisible = shell.isInspectorVisible()
        self.inspectorHiddenByUser = bool(getattr(shell, "_inspectorHiddenByUser", False))
        self.focusMode = shell.focusMode()
        focusWidget = slicer.app.focusWidget()
        self.focusWidget = focusWidget if focusWidget is not None and focusWidget.window() == shell.mainWindow else None
        self.settings = self._studioSettings()
        #: Studio/* keys written by what the tour itself did (switching workspaces, showing the inspector, restoring)
        self.touchedKeys = set()
        self.nodeIds = {node.GetID() for node in self._nodes()}
        self.sceneModified = slicer.mrmlScene.GetModifiedSinceRead()

    @staticmethod
    def _nodes():
        nodes = []
        for index in range(slicer.mrmlScene.GetNumberOfNodes()):
            node = slicer.mrmlScene.GetNthNode(index)
            if node is not None:
                nodes.append(node)
        return nodes

    @staticmethod
    def _studioSettings():
        settings = qt.QSettings()
        return {key: settings.value(key) for key in settings.allKeys() if key.startswith("Studio/") and not key.startswith(_TOUR_GROUP)}

    @staticmethod
    def _viewKeys():
        from . import workspaces

        return set(_VIEW_KEYS) | {ws.layoutSettingsKey for ws in workspaces.WORKSPACES}

    @contextlib.contextmanager
    def tracking(self):
        """Record the Studio/* keys written inside this block: they are put back when the tour ends."""
        before = self._studioSettings()
        try:
            yield
        finally:
            after = self._studioSettings()
            for key in set(before) | set(after):
                if key not in before or key not in after or not _sameValue(before[key], after[key]):
                    self.touchedKeys.add(key)

    def prepareTour(self):
        """Leave the hidden panels (the top bar must be visible to show it)."""
        if self.focusMode:
            with self.tracking():
                self.shell.setFocusMode(False)

    def restore(self, views=True):
        """Put everything back. ``views=False`` (the shell goes Classic) restores only the scene and the settings."""
        shell = self.shell
        if views and shell.isActive():
            try:
                with self.tracking():
                    self._restoreViews()
            except Exception:
                logging.error(f"Studio tour: cannot restore the workspace:\n{traceback.format_exc()}")
        try:
            self.cleanScene()
        except Exception:
            logging.error(f"Studio tour: cannot restore the scene:\n{traceback.format_exc()}")
        if views and shell.isActive():
            try:
                with self.tracking():
                    self._restoreInspector()
            except Exception:
                logging.error(f"Studio tour: cannot restore the inspector:\n{traceback.format_exc()}")
        self._restoreSettings()
        if views and self.focusWidget is not None and _isAlive(self.focusWidget) and self.focusWidget.isVisible():
            self.focusWidget.setFocus(qt.Qt.OtherFocusReason)

    def _restoreViews(self):
        shell = self.shell
        if self.transientModule:
            origin = self.transientOrigin or self.workspace
            if origin and shell.currentWorkspace() != origin and shell.transientOrigin() != origin:
                shell.switchTo(origin)
            if shell.currentModuleName() != self.transientModule:
                slicer.util.selectModule(self.transientModule)
        elif self.workspace and shell.currentWorkspace() != self.workspace:
            shell.switchTo(self.workspace)
        layoutManager = slicer.app.layoutManager()
        if self.layout is not None and layoutManager is not None and layoutManager.layout != self.layout:
            layoutManager.setLayout(self.layout)
        if self.focusMode and not shell.focusMode():
            shell.setFocusMode(True)

    def _restoreInspector(self):
        shell = self.shell
        if self.focusMode:
            return
        if shell.isInspectorVisible() != self.inspectorVisible:
            shell.setInspectorVisible(self.inspectorVisible)
        if hasattr(shell, "_inspectorHiddenByUser"):
            shell._inspectorHiddenByUser = self.inspectorHiddenByUser
        if hasattr(shell, "_updateInspectorHandle"):
            shell._updateInspectorHandle()

    def cleanScene(self):
        """Remove the empty segmentations that showing Draw created (the Segment Editor creates one on an empty scene)."""
        for node in self._nodes():
            if node.GetID() in self.nodeIds or not any(node.IsA(className) for className in _GUARDED_NODE_CLASSES):
                continue
            segmentation = node.GetSegmentation() if hasattr(node, "GetSegmentation") else None
            if segmentation is None or segmentation.GetNumberOfSegments() > 0 or node.GetStorageNode() is not None:
                continue
            slicer.mrmlScene.RemoveNode(node)

    def _restoreSettings(self):
        """Put back the keys that the tour wrote and the view keys; leave every other setting as it is now."""
        settings = qt.QSettings()
        current = self._studioSettings()
        for key in (self.touchedKeys | self._viewKeys()):
            if key.startswith(_TOUR_GROUP):
                continue
            if key not in self.settings:
                if key in current:
                    settings.remove(key)
            elif key not in current or not _sameValue(current[key], self.settings[key]):
                settings.setValue(key, self.settings[key])


#
# Overlay, spotlight and card
#


class _Overlay(qt.QWidget):
    """Dims the main window except a rounded spotlight around the control, with an accent ring.

    It covers the whole main window and takes every click and wheel event on the dimmed area. On a "Try it" step
    the spotlight is cut out of its mask, so that the control receives the clicks.
    """

    def __init__(self, parent):
        qt.QWidget.__init__(self, parent)
        self.setObjectName("StudioTourOverlay")
        self.setAttribute(qt.Qt.WA_NoSystemBackground)
        self.setFocusPolicy(qt.Qt.NoFocus)
        self.setAcceptDrops(True)
        self.spot = None
        self.passThrough = False

    def setSpot(self, rect, passThrough=False):
        self.spot = qt.QRect(rect) if rect is not None else None
        self.passThrough = passThrough
        self._updateMask()
        self.update()

    def _updateMask(self):
        if not self.passThrough or self.spot is None:
            self.clearMask()
            return
        hole = qt.QPainterPath()
        hole.addRoundedRect(qt.QRectF(self.spot), SPOTLIGHT_RADIUS, SPOTLIGHT_RADIUS)
        try:
            holeRegion = qt.QRegion(hole.toFillPolygon(qt.QTransform()).toPolygon())
        except Exception:
            holeRegion = qt.QRegion(self.spot)
        self.setMask(qt.QRegion(self.rect).subtracted(holeRegion))

    def paintEvent(self, event):
        painter = qt.QPainter(self)
        try:
            painter.setRenderHint(qt.QPainter.Antialiasing)
            dim = qt.QColor(0, 0, 0)
            dim.setAlphaF(DIM_ALPHA_DARK if ui.isDark() else DIM_ALPHA_LIGHT)
            path = qt.QPainterPath()
            path.addRect(qt.QRectF(self.rect))
            if self.spot is not None:
                hole = qt.QPainterPath()
                hole.addRoundedRect(qt.QRectF(self.spot), SPOTLIGHT_RADIUS, SPOTLIGHT_RADIUS)
                path = path.subtracted(hole)
            painter.fillPath(path, qt.QBrush(dim))
            if self.spot is not None:
                pen = qt.QPen(ui.token("accent-fg"))
                pen.setWidthF(RING_WIDTH)
                painter.setPen(pen)
                painter.setBrush(qt.QBrush(qt.Qt.NoBrush))
                ring = qt.QRectF(self.spot).adjusted(-RING_WIDTH / 2.0, -RING_WIDTH / 2.0, RING_WIDTH / 2.0, RING_WIDTH / 2.0)
                painter.drawRoundedRect(ring, SPOTLIGHT_RADIUS + 1, SPOTLIGHT_RADIUS + 1)
        finally:
            painter.end()

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    # Files dropped on the dimmed area are not loaded during a tour (the drop is taken here and ignored)

    def dragEnterEvent(self, event):
        _refuseDrop(event)

    def dragMoveEvent(self, event):
        _refuseDrop(event)

    def dropEvent(self, event):
        _refuseDrop(event)


class _Dots(qt.QWidget):
    """Progress dots: one per step, the current one wider and in the accent color."""

    DOT = 6
    CURRENT = 16
    SPACING = 5

    def __init__(self, parent=None):
        qt.QWidget.__init__(self, parent)
        self.setObjectName("StudioTourDots")
        self.count = 0
        self.current = 0
        self.setFixedHeight(self.DOT + 2)

    def setProgress(self, current, count):
        self.current, self.count = current, count
        width = 0 if count <= 0 else (count - 1) * (self.DOT + self.SPACING) + self.CURRENT
        self.setFixedWidth(max(1, width))
        self.update()

    def paintEvent(self, event):
        painter = qt.QPainter(self)
        try:
            painter.setRenderHint(qt.QPainter.Antialiasing)
            painter.setPen(qt.QPen(qt.Qt.NoPen))
            x = 0.0
            for index in range(self.count):
                current = index == self.current
                width = self.CURRENT if current else self.DOT
                color = ui.token("accent-fg") if current else (ui.token("control-border") if index < self.current else ui.token("border"))
                painter.setBrush(qt.QBrush(color))
                painter.drawRoundedRect(qt.QRectF(x, 1, width, self.DOT), self.DOT / 2.0, self.DOT / 2.0)
                x += width + self.SPACING
        finally:
            painter.end()


class _CardFrame(qt.QWidget):
    """The card: a rounded surface with a 1 px border and a pointer towards the spotlight (``pointerSide``)."""

    def __init__(self, parent):
        qt.QWidget.__init__(self, parent)
        self.setObjectName("StudioTourCard")
        self.setAcceptDrops(True)
        self.pointerSide = None
        self.pointerOffset = 0
        self.box = qt.QRect()

    def paintEvent(self, event):
        painter = qt.QPainter(self)
        try:
            painter.setRenderHint(qt.QPainter.Antialiasing)
            box = qt.QRectF(self.box).adjusted(0.5, 0.5, -0.5, -0.5)
            path = qt.QPainterPath()
            path.addRoundedRect(box, CARD_RADIUS, CARD_RADIUS)
            if self.pointerSide is not None:
                path = path.united(self._pointer(box))
            painter.fillPath(path, qt.QBrush(ui.token("card")))
            pen = qt.QPen(ui.token("border"))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.setBrush(qt.QBrush(qt.Qt.NoBrush))
            painter.drawPath(path.simplified())
        finally:
            painter.end()

    def _pointer(self, box):
        size = POINTER_SIZE
        offset = self.pointerOffset
        if self.pointerSide == "left":
            points = ((box.left() + 1, offset - size), (box.left() - size, offset), (box.left() + 1, offset + size))
        elif self.pointerSide == "right":
            points = ((box.right() - 1, offset - size), (box.right() + size, offset), (box.right() - 1, offset + size))
        elif self.pointerSide == "top":
            points = ((offset - size, box.top() + 1), (offset, box.top() - size), (offset + size, box.top() + 1))
        else:
            points = ((offset - size, box.bottom() - 1), (offset, box.bottom() + size), (offset + size, box.bottom() - 1))
        # Lines rather than a QPolygonF (a list type that PythonQt wraps differently with Qt 5 and Qt 6)
        path = qt.QPainterPath()
        path.moveTo(points[0][0], points[0][1])
        for x, y in points[1:]:
            path.lineTo(x, y)
        path.closeSubpath()
        return path

    # Files dropped on the card are not loaded either (they would reach the main window)

    def dragEnterEvent(self, event):
        _refuseDrop(event)

    def dragMoveEvent(self, event):
        _refuseDrop(event)

    def dropEvent(self, event):
        _refuseDrop(event)


class _CardKeys(qt.QObject):
    """Keys of the card, on the card and its buttons: Right goes on, Left back, Esc leaves; Enter and Space press the
    focused button (Next by default); Tab and Shift+Tab stay inside the card. The shortcut override of these keys is
    accepted, so that window shortcuts on them (the Segment Editor's Space and Esc) do not fire.
    """

    _KEYS = (qt.Qt.Key_Right, qt.Qt.Key_Left, qt.Qt.Key_Escape, qt.Qt.Key_Return, qt.Qt.Key_Enter, qt.Qt.Key_Space,
             qt.Qt.Key_Tab, qt.Qt.Key_Backtab)

    def __init__(self, card):
        qt.QObject.__init__(self)
        self.card = card

    def eventFilter(self, obj, event):
        eventType = event.type()
        if eventType not in (qt.QEvent.ShortcutOverride, qt.QEvent.KeyPress):
            return False
        key = event.key()
        if key not in self._KEYS:
            return False
        if ui.hasKeyModifiers(event) and key not in (qt.Qt.Key_Tab, qt.Qt.Key_Backtab):
            return False
        if eventType == qt.QEvent.ShortcutOverride:
            event.accept()
            return True
        self.card.onKey(key, obj)
        return True


class _Card:
    """The callout card of a step and its controls."""

    def __init__(self, player, parent):
        self.player = player
        self.frame = _CardFrame(parent)
        self.content = qt.QWidget(self.frame)
        self.content.setObjectName("StudioTourCardContent")
        layout = qt.QVBoxLayout(self.content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header: step counter and progress dots
        header = qt.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.counterLabel = _label("", "StudioTourStepCounter", 11, qt.QFont.DemiBold, wrap=False)
        ui._font(self.counterLabel, ui.fontPx(11), qt.QFont.DemiBold, 0.6)
        header.addWidget(self.counterLabel)
        header.addStretch(1)
        self.dots = _Dots()
        header.addWidget(self.dots, 0, qt.Qt.AlignVCenter)
        layout.addLayout(header)
        layout.addSpacing(8)
        self.titleLabel = _label("", "StudioTourTitle", 17, qt.QFont.DemiBold)
        layout.addWidget(self.titleLabel)
        layout.addSpacing(6)
        self.bodyLabel = _label("", "StudioTourBody", 13)
        layout.addWidget(self.bodyLabel)
        layout.addSpacing(12)

        # How it is done in official 3D Slicer
        self.slicerBox = qt.QFrame()
        self.slicerBox.setObjectName("StudioTourInSlicer")
        slicerLayout = qt.QVBoxLayout(self.slicerBox)
        slicerLayout.setContentsMargins(12, 9, 12, 10)
        slicerLayout.setSpacing(3)
        self.slicerCaption = _label(_("In 3D Slicer").upper(), "StudioTourInSlicerCaption", 11, qt.QFont.DemiBold, wrap=False)
        ui._font(self.slicerCaption, ui.fontPx(10.5), qt.QFont.DemiBold, 0.6)
        slicerLayout.addWidget(self.slicerCaption)
        self.slicerLabel = _label("", "StudioTourInSlicerText", 12.5)
        slicerLayout.addWidget(self.slicerLabel)
        layout.addWidget(self.slicerBox)

        # Keys and "Try it"
        self.keysSpacing = qt.QWidget()
        self.keysSpacing.setFixedHeight(10)
        layout.addWidget(self.keysSpacing)
        self.keysWidget = qt.QWidget()
        self.keysWidget.setObjectName("StudioTourKeys")
        self.keysLayout = qt.QVBoxLayout(self.keysWidget)
        self.keysLayout.setContentsMargins(0, 0, 0, 0)
        self.keysLayout.setSpacing(6)
        self._keyRows = []
        layout.addWidget(self.keysWidget)
        self.tryItSpacing = qt.QWidget()
        self.tryItSpacing.setFixedHeight(10)
        layout.addWidget(self.tryItSpacing)
        tryItRow = qt.QWidget()
        tryItRow.setObjectName("StudioTourTryIt")
        tryItLayout = qt.QHBoxLayout(tryItRow)
        tryItLayout.setContentsMargins(0, 0, 0, 0)
        tryItLayout.setSpacing(6)
        self.tryItIcon = qt.QLabel()
        self.tryItIcon.setObjectName("StudioTourTryItIcon")
        tryItLayout.addWidget(self.tryItIcon, 0, qt.Qt.AlignTop)
        self.tryItLabel = _label("", "StudioTourTryItText", 12.5, qt.QFont.Medium)
        tryItLayout.addWidget(self.tryItLabel, 1)
        self.tryItRow = tryItRow
        layout.addWidget(tryItRow)
        layout.addSpacing(16)

        # Footer: Skip tour, Back, Next
        footer = qt.QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        self.skipButton = qt.QPushButton(_("Skip tour"))
        self.skipButton.setObjectName("StudioTourSkipButton")
        self.skipButton.setFlat(True)
        self.skipButton.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.skipButton.setToolTip(ui.tooltip(_("Skip tour"), "Esc", _("Leave the tour; everything is put back as it was")))
        footer.addWidget(self.skipButton)
        footer.addStretch(1)
        self.backButton = qt.QPushButton(_("Back"))
        self.backButton.setObjectName("StudioTourBackButton")
        ui.setRole(self.backButton, "secondary")
        self.backButton.setMinimumHeight(30)
        self.backButton.setToolTip(ui.tooltip(_("Back"), "Left"))
        footer.addWidget(self.backButton)
        self.nextButton = qt.QPushButton(_("Next"))
        self.nextButton.setObjectName("StudioTourNextButton")
        ui.setRole(self.nextButton, "primary")
        self.nextButton.setMinimumHeight(30)
        self.nextButton.setToolTip(ui.tooltip(_("Next"), "Right"))
        footer.addWidget(self.nextButton)
        for button in (self.skipButton, self.backButton, self.nextButton):
            button.setFocusPolicy(qt.Qt.StrongFocus)
            button.setAutoDefault(False)
            _font(button, 13, qt.QFont.DemiBold if button is self.nextButton else None)
        layout.addLayout(footer)

        self.skipButton.connect("clicked()", self.player.stop)
        self.backButton.connect("clicked()", self.player.back)
        self.nextButton.connect("clicked()", self.player.next)

        self._keys = _CardKeys(self)
        for widget in (self.frame, self.content, self.skipButton, self.backButton, self.nextButton):
            widget.installEventFilter(self._keys)
        ui.bindStyleSheet(self.frame, self._styleSheet)
        self.frame.hide()

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("text", "text2", "text3", "accent-fg", "control-hover", "canvas", "hairline")}
        slicerBackground = t["canvas"] if ui.isDark() else t["control-hover"]
        return (
            "QWidget#StudioTourCardContent, QWidget#StudioTourKeys, QWidget#StudioTourTryIt { background: transparent; }"
            f"QLabel#StudioTourStepCounter {{ color: {t['text3']}; background: transparent; }}"
            f"QLabel#StudioTourTitle {{ color: {t['text']}; background: transparent; }}"
            f"QLabel#StudioTourBody {{ color: {t['text']}; background: transparent; }}"
            f"QFrame#StudioTourInSlicer {{ background-color: {slicerBackground}; border: 1px solid {t['hairline']}; border-radius: 8px; }}"
            f"QLabel#StudioTourInSlicerCaption {{ color: {t['text3']}; background: transparent; border: none; }}"
            f"QLabel#StudioTourInSlicerText {{ color: {t['text2']}; background: transparent; border: none; }}"
            f"QLabel#StudioTourKeyText {{ color: {t['text2']}; background: transparent; }}"
            f"QLabel#StudioTourTryItText {{ color: {t['accent-fg']}; background: transparent; }}"
            f"QPushButton#StudioTourSkipButton {{ background: transparent; border: none; color: {t['text2']}; padding: 4px 8px; }}"
            f"QPushButton#StudioTourSkipButton:hover {{ color: {t['text']}; text-decoration: underline; }}"
            f"QPushButton#StudioTourSkipButton:focus {{ color: {t['text']}; text-decoration: underline; }}")

    def _setKeys(self, keys):
        self._keyRows = []
        while self.keysLayout.count():
            item = self.keysLayout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        for keySequences, text in keys:
            row = qt.QWidget()
            rowLayout = qt.QHBoxLayout(row)
            rowLayout.setContentsMargins(0, 0, 0, 0)
            rowLayout.setSpacing(4)
            for index, key in enumerate(keySequences):
                if index:
                    separator = _label("\u2013" if len(keySequences) == 2 and keySequences in _KEY_RANGES else "/", "StudioTourKeyText", 12, wrap=False)
                    rowLayout.addWidget(separator)
                chip = ui.kbd(_migration().keyText(key))
                chip.setObjectName("StudioTourKey")
                chip.setMinimumHeight(ui.fontPx(20))
                rowLayout.addWidget(chip)
            description = _label(text, "StudioTourKeyText", 12.5)
            rowLayout.addSpacing(4)
            rowLayout.addWidget(description, 1)
            self._keyRows.append(row)
            self.keysLayout.addWidget(row)
            # Now, not on the next event loop iteration (as a layout would): the card is measured right away
            row.show()
        self.keysWidget.setVisible(bool(keys))
        self.keysSpacing.setVisible(bool(keys))

    def setStep(self, step, shell, index, count, first, last, finishText=None, single=False):
        if single:
            self.counterLabel.setText(_("Where did it go?").upper())
        else:
            self.counterLabel.setText(_("Step {index} of {count}").format(index=index + 1, count=count).upper())
        self.titleLabel.setText(step.title)
        self.bodyLabel.setText(step.bodyText(shell))
        inSlicer = step.inSlicerText()
        self.slicerLabel.setText(inSlicer)
        self.slicerBox.setVisible(bool(inSlicer))
        self._setKeys(step.keys)
        self.tryItRow.setVisible(bool(step.tryIt))
        self.tryItSpacing.setVisible(bool(step.tryIt))
        if step.tryIt:
            self.tryItLabel.setText(_("Try it: {action}").format(action=step.tryIt))
            self.tryItIcon.setPixmap(ui.pixmap("mouse-pointer-2", ui.fontPx(14), "accent-fg"))
        self.dots.setVisible(not single)
        self.dots.setProgress(index, count)
        self.skipButton.setVisible(not single)
        self.backButton.setVisible(not single)
        self.backButton.setEnabled(not first)
        if last:
            self.nextButton.setText(finishText or _("Done"))
        else:
            self.nextButton.setText(_("Next"))
        accessible = f"{step.title}. {step.bodyText(shell)} {_('In 3D Slicer')}: {inSlicer}"
        self.frame.setAccessibleName(step.title)
        self.frame.setAccessibleDescription(accessible)

    def width(self):
        return ui.fontPx(CARD_WIDTH)

    def boxHeight(self, width):
        innerWidth = width - 2 * CARD_PADDING
        self.content.setFixedWidth(innerWidth)
        # Fonts of the role stylesheets (key chips) and the size hints of the rows that changed, before measuring
        self.frame.ensurePolished()
        for widget in [*self._keyRows, self.keysWidget, self.slicerBox, self.tryItRow]:
            if widget.layout() is not None:
                widget.layout().invalidate()
            widget.updateGeometry()
        layout = self.content.layout()
        layout.invalidate()
        layout.activate()
        height = layout.totalHeightForWidth(innerWidth) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        return height + 2 * CARD_PADDING

    def place(self, box, pointerSide, pointerOffset):
        """Show the card with its box at ``box`` (main window coordinates) and the pointer on ``pointerSide``."""
        widgetRect = qt.QRect(box)
        if pointerSide == "left":
            widgetRect.setLeft(box.left() - POINTER_SIZE)
        elif pointerSide == "right":
            widgetRect.setRight(box.right() + POINTER_SIZE)
        elif pointerSide == "top":
            widgetRect.setTop(box.top() - POINTER_SIZE)
        elif pointerSide == "bottom":
            widgetRect.setBottom(box.bottom() + POINTER_SIZE)
        self.frame.setGeometry(widgetRect)
        localBox = qt.QRect(box.translated(-widgetRect.left(), -widgetRect.top()))
        self.frame.box = localBox
        self.frame.pointerSide = pointerSide
        if pointerSide in ("left", "right"):
            self.frame.pointerOffset = pointerOffset - widgetRect.top()
        else:
            self.frame.pointerOffset = pointerOffset - widgetRect.left()
        self.content.setGeometry(localBox.adjusted(CARD_PADDING, CARD_PADDING, -CARD_PADDING, -CARD_PADDING))
        self.frame.show()
        self.frame.raise_()
        self.frame.update()

    def boxRect(self):
        """The card surface in main window coordinates (without the pointer)."""
        return self.frame.box.translated(self.frame.x, self.frame.y)

    def focusNext(self):
        self.nextButton.setFocus(qt.Qt.OtherFocusReason)

    def onKey(self, key, obj):
        if key == qt.Qt.Key_Escape:
            self.player.stop()
        elif key == qt.Qt.Key_Right:
            self.player.next()
        elif key == qt.Qt.Key_Left:
            if self.backButton.isVisible() and self.backButton.isEnabled():
                self.player.back()
        elif key in (qt.Qt.Key_Return, qt.Qt.Key_Enter, qt.Qt.Key_Space):
            focused = obj if obj in (self.skipButton, self.backButton, self.nextButton) else None
            if focused is not None and focused.isEnabled() and focused.isVisible():
                focused.click()
            else:
                self.player.next()
        elif key in (qt.Qt.Key_Tab, qt.Qt.Key_Backtab):
            buttons = [button for button in (self.skipButton, self.backButton, self.nextButton) if button.isVisible() and button.isEnabled()]
            if not buttons:
                return
            current = slicer.app.focusWidget()
            index = buttons.index(current) if current in buttons else len(buttons) - 1
            index = (index + (-1 if key == qt.Qt.Key_Backtab else 1)) % len(buttons)
            buttons[index].setFocus(qt.Qt.TabFocusReason)

    def dispose(self):
        ui.bindStyleSheet(self.frame, None)
        for widget in (self.frame, self.content, self.skipButton, self.backButton, self.nextButton):
            widget.removeEventFilter(self._keys)
        self.frame.hide()
        self.frame.setObjectName("")
        self.frame.deleteLater()


def _clamp(value, low, high):
    return max(low, min(value, high)) if high >= low else low


def placeCard(spot, width, height, area, preferred="auto"):
    """Where the card goes: ``(box, pointerSide, pointerOffset)`` in main window coordinates.

    The card goes right of, below, left of or above the spotlight (``preferred`` first), the first side where it
    fits inside ``area`` without covering the spotlight. Without such a side it goes to the corner of ``area`` farthest
    from the spotlight, without a pointer.
    """
    sides = ("right", "below", "left", "above")
    order = [preferred] + [side for side in sides if side != preferred] if preferred in sides else list(sides)
    distance = CARD_GAP + POINTER_SIZE
    for side in order:
        if side == "right":
            x = spot.right() + 1 + distance
            y = _clamp(spot.center().y() - height // 2, area.top(), area.bottom() + 1 - height)
            fits = x + width <= area.right() + 1 and height <= area.height()
            pointer = ("left", _clamp(spot.center().y(), y + CARD_RADIUS + POINTER_SIZE, y + height - CARD_RADIUS - POINTER_SIZE))
        elif side == "left":
            x = spot.left() - distance - width
            y = _clamp(spot.center().y() - height // 2, area.top(), area.bottom() + 1 - height)
            fits = x >= area.left() and height <= area.height()
            pointer = ("right", _clamp(spot.center().y(), y + CARD_RADIUS + POINTER_SIZE, y + height - CARD_RADIUS - POINTER_SIZE))
        elif side == "below":
            y = spot.bottom() + 1 + distance
            x = _clamp(spot.center().x() - width // 2, area.left(), area.right() + 1 - width)
            fits = y + height <= area.bottom() + 1 and width <= area.width()
            pointer = ("top", _clamp(spot.center().x(), x + CARD_RADIUS + POINTER_SIZE, x + width - CARD_RADIUS - POINTER_SIZE))
        else:
            y = spot.top() - distance - height
            x = _clamp(spot.center().x() - width // 2, area.left(), area.right() + 1 - width)
            fits = y >= area.top() and width <= area.width()
            pointer = ("bottom", _clamp(spot.center().x(), x + CARD_RADIUS + POINTER_SIZE, x + width - CARD_RADIUS - POINTER_SIZE))
        if fits:
            return qt.QRect(int(x), int(y), int(width), int(height)), pointer[0], int(pointer[1])
    # No side has room: the corner farthest from the spotlight
    x = area.left() if spot.center().x() > area.center().x() else area.right() + 1 - width
    y = area.top() if spot.center().y() > area.center().y() else area.bottom() + 1 - height
    return qt.QRect(int(max(area.left(), x)), int(max(area.top(), y)), int(width), int(height)), None, 0


class _SingleStepTour:
    """A tour of one step, built for "Show me" from a 'Where did it go?' row."""

    def __init__(self, step, title):
        self.id = f"show-me:{step.id}"
        self.title = title
        self.description = ""
        self.steps = [step]
        self.workspace = None
        self.finishText = None

    def isAvailable(self, shell):
        return True


class TourPlayer:
    """Plays a tour over the main window (see the module documentation)."""

    def __init__(self, shell, tour, onFinished=None, single=False, finishText=None):
        self.shell = shell
        self.tour = tour
        self.onFinished = onFinished
        self.single = single
        self.finishText = finishText or getattr(tour, "finishText", None)
        self.index = -1
        self.target = None
        self.skipped = set()
        self.active = False
        self.completed = False
        self._state = None
        self._overlay = None
        self._card = None
        self._timer = None
        self._eventFilter = None
        self._spot = None
        self._lastLayout = None
        #: True while a step is prepared (the events processed then must not move the tour)
        self._busy = False

    #
    # Start, stop
    #

    def start(self):
        shell = self.shell
        if self.active or not shell.isActive() or _modalOpen():
            return False
        self._state = _ViewState(shell)
        self._state.prepareTour()
        mainWindow = shell.mainWindow
        self._overlay = _Overlay(mainWindow)
        self._overlay.setGeometry(mainWindow.rect)
        self._card = _Card(self, mainWindow)
        self._eventFilter = _MainWindowWatcher(self)
        mainWindow.installEventFilter(self._eventFilter)
        self._timer = qt.QTimer()
        self._timer.setInterval(FOLLOW_INTERVAL_MS)
        self._timer.connect("timeout()", self._follow)
        self.active = True
        shell.registerEscapeOverlay(self._overlay, self.stop)
        self._overlay.show()
        self._overlay.raise_()
        if not self._goTo(0, 1):
            self._finish(completed=False)
            return False
        self._timer.start()
        return True

    def stop(self):
        """Leave the tour (Esc, Skip tour): everything is put back as it was."""
        self._finish(completed=False)

    def _finish(self, completed, restoreViews=True):
        if not self.active:
            return
        self.active = False
        self.completed = completed
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        shell = self.shell
        try:
            shell.unregisterEscapeOverlay(self._overlay)
        except Exception:
            pass
        if self._eventFilter is not None:
            shell.mainWindow.removeEventFilter(self._eventFilter)
            self._setTarget(None)
            self._eventFilter = None
        if self._card is not None:
            self._card.dispose()
            self._card = None
        if self._overlay is not None:
            self._overlay.hide()
            self._overlay.setObjectName("")
            self._overlay.deleteLater()
            self._overlay = None
        if self._state is not None:
            self._state.restore(views=restoreViews)
            self._state = None
        if completed and not self.single:
            _markCompleted(self.tour.id)
        if self.onFinished is not None and restoreViews:
            callback = self.onFinished
            self.onFinished = None
            qt.QTimer.singleShot(0, lambda: callback(self))

    def abandon(self):
        """The shell goes Classic: remove the tour without showing workspaces again."""
        self.onFinished = None
        self._finish(completed=False, restoreViews=False)

    #
    # Navigation
    #

    def visibleSteps(self):
        """The steps that will probably be shown (for the counter and the dots)."""
        return [index for index, step in enumerate(self.tour.steps)
                if index not in self.skipped and (index == self.index or _stepMayShow(step, self.shell))]

    def next(self):
        if not self.active:
            return
        if not self._goTo(self.index + 1, 1):
            self._finish(completed=True)

    def back(self):
        if not self.active or self.index <= 0:
            return
        self._goTo(self.index - 1, -1)

    def _goTo(self, index, direction):
        """Show the first step from ``index`` in ``direction`` that can be shown. Return False if there is none."""
        steps = self.tour.steps
        self._busy = True
        try:
            while 0 <= index < len(steps):
                step = steps[index]
                if index not in self.skipped and self._prepare(step):
                    target = resolveTarget(step.target, self.shell)
                    if target is not None:
                        self.index = index
                        self._setTarget(target)
                        self._showStep()
                        return True
                self.skipped.add(index)
                index += direction
            return False
        finally:
            self._busy = False

    def _prepare(self, step):
        """Show the workspace and the inspector that the step needs. Return False if the step cannot be shown."""
        shell = self.shell
        if step.when is not None:
            try:
                if not step.when(shell):
                    return False
            except Exception:
                logging.error(f"Studio tour: condition of step {step.id} failed:\n{traceback.format_exc()}")
                return False
        if self._state is None:
            return self._prepareViews(step)
        # The settings that showing the workspace and the inspector writes are put back when the tour ends
        with self._state.tracking():
            return self._prepareViews(step)

    def _prepareViews(self, step):
        shell = self.shell
        if step.workspace is not None:
            from . import workspaces

            ws = workspaces.workspace(step.workspace)
            if ws is None or not workspaces.isAvailable(ws):
                return False
            if shell.currentWorkspace() != ws.key:
                shell.switchTo(ws.key)
                if ws.key != "draw" and self._state is not None:
                    # Draw shown on an empty scene created an empty segmentation: remove it now, so that Library
                    # does not show it as the user's data
                    self._state.cleanScene()
            if shell.currentWorkspace() != ws.key:
                return False
        if step.inspector and not shell.isInspectorVisible():
            shell.setInspectorVisible(True)
        # Show events and layouts of what was just shown
        try:
            slicer.app.processEvents(qt.QEventLoop.ExcludeUserInputEvents)
        except TypeError:
            # PythonQt without the enum-to-QFlags conversion (Qt 6)
            slicer.app.processEvents()
        return True

    def currentStep(self):
        return self.tour.steps[self.index] if 0 <= self.index < len(self.tour.steps) else None

    def _setTarget(self, target):
        """Follow ``target``: it is watched for resizes and moves (the timer also follows its ancestors)."""
        if self.target is not None and _isAlive(self.target) and self._eventFilter is not None:
            self.target.removeEventFilter(self._eventFilter)
        self.target = target
        if target is not None and self._eventFilter is not None:
            target.installEventFilter(self._eventFilter)
        _scrollIntoView(target)

    def _showStep(self):
        step = self.currentStep()
        visible = self.visibleSteps()
        position = visible.index(self.index) if self.index in visible else 0
        last = not any(index > self.index for index in visible)
        self._card.setStep(step, self.shell, position, len(visible), first=(position == 0), last=last,
                           finishText=self.finishText, single=self.single)
        self._spot = None
        self._layout(force=True)
        self._card.focusNext()

    def _spotRect(self):
        mainWindow = self.shell.mainWindow
        rect = _rectIn(self.target, mainWindow).adjusted(-SPOTLIGHT_PADDING, -SPOTLIGHT_PADDING, SPOTLIGHT_PADDING, SPOTLIGHT_PADDING)
        return rect.intersected(mainWindow.rect.adjusted(RING_WIDTH, RING_WIDTH, -RING_WIDTH, -RING_WIDTH))

    def _layout(self, force=False):
        if not self.active or self.target is None or (self._busy and not force):
            return
        mainWindow = self.shell.mainWindow
        spot = self._spotRect()
        windowRect = qt.QRect(mainWindow.rect)
        signature = (spot.x(), spot.y(), spot.width(), spot.height(), windowRect.width(), windowRect.height())
        if not force and signature == self._lastLayout:
            return
        self._lastLayout = signature
        self._overlay.setGeometry(windowRect)
        step = self.currentStep()
        self._overlay.setSpot(spot, passThrough=bool(step is not None and step.tryIt))
        self._overlay.raise_()
        width = min(self._card.width(), windowRect.width() - 2 * WINDOW_MARGIN)
        height = self._card.boxHeight(width)
        area = windowRect.adjusted(WINDOW_MARGIN, WINDOW_MARGIN, -WINDOW_MARGIN, -WINDOW_MARGIN)
        box, pointerSide, pointerOffset = placeCard(spot, width, height, area, step.placement if step is not None else "auto")
        self._card.place(box, pointerSide, pointerOffset)
        self._spot = spot

    def _follow(self):
        """Follow the control (moves, docks, layouts), keep the overlay on top and check a "Try it" step."""
        if not self.active or self._busy:
            return
        if not self.shell.isActive():
            self.abandon()
            return
        step = self.currentStep()
        if step is not None and step.tryIt and step.done is not None:
            try:
                done = step.done(self.shell)
            except Exception:
                done = False
            if done:
                self.next()
                return
        if self.target is None or not _isAlive(self.target) or not _isShown(self.target, self.shell.mainWindow):
            # The control went away (a key switched the workspace): show the step again, or the next one
            self._busy = True
            try:
                target = resolveTarget(step.target, self.shell) if step is not None and self._prepare(step) else None
            finally:
                self._busy = False
            if not self.active:
                return
            if target is None:
                if not self._goTo(self.index + 1, 1):
                    self._finish(completed=True)
                return
            self._setTarget(target)
        self._layout()
        # A dock or toolbar shown meanwhile is stacked above the overlay
        children = [child for child in self.shell.mainWindow.children() if child.isWidgetType() and not child.isHidden()]
        if children and children[-1] != self._card.frame:
            self._overlay.raise_()
            self._card.frame.raise_()

    #
    # Introspection (tests)
    #

    def spotlightRect(self):
        return qt.QRect(self._spot) if self._spot is not None else None

    def cardRect(self):
        return self._card.boxRect() if self._card is not None else None

    def card(self):
        return self._card

    def overlay(self):
        return self._overlay


class _MainWindowWatcher(qt.QObject):
    """Lays the tour out again when the main window or the control is resized or moved."""

    _EVENTS = (qt.QEvent.Resize, qt.QEvent.Move, qt.QEvent.LayoutRequest, qt.QEvent.Show, qt.QEvent.Hide)

    def __init__(self, player):
        qt.QObject.__init__(self)
        self.player = player

    def eventFilter(self, obj, event):
        if event.type() in self._EVENTS:
            qt.QTimer.singleShot(0, self.player._layout)
        return False


#
# 'Where did it go?' panel
#


class WhereMap:
    """The 'Where did it go?' panel: a searchable card over the inspector, non-modal (``StudioWhereMap``)."""

    def __init__(self, plugin):
        self.plugin = plugin
        shell = plugin.shell
        mainWindow = shell.mainWindow
        self.frame = qt.QFrame(mainWindow)
        self.frame.setObjectName("StudioWhereMap")
        self.frame.setAccessibleName(_("Where did it go?"))
        layout = qt.QVBoxLayout(self.frame)
        layout.setContentsMargins(18, 16, 12, 14)
        layout.setSpacing(0)

        header = qt.QHBoxLayout()
        header.setSpacing(8)
        self.icon = qt.QLabel()
        self.icon.setObjectName("StudioWhereIcon")
        header.addWidget(self.icon, 0, qt.Qt.AlignVCenter)
        title = _label(_("Where did it go?"), "StudioWhereTitle", 16, qt.QFont.DemiBold, wrap=False)
        header.addWidget(title, 1)
        self.closeButton = qt.QToolButton()
        self.closeButton.setObjectName("StudioWhereCloseButton")
        self.closeButton.setAutoRaise(True)
        self.closeButton.setFixedSize(26, 26)
        self.closeButton.setIconSize(qt.QSize(16, 16))
        self.closeButton.setToolTip(ui.tooltip(_("Close"), "Esc"))
        ui.setRole(self.closeButton, "ghost")
        ui.installButtonKeys(self.closeButton)
        self.closeButton.connect("clicked()", self.close)
        header.addWidget(self.closeButton)
        layout.addLayout(header)
        layout.addSpacing(4)
        subtitle = _label(_("Where the places you know from 3D Slicer are in Slicer Studio."), "StudioWhereSubtitle", 12.5)
        subtitle.setContentsMargins(0, 0, 6, 0)
        layout.addWidget(subtitle)
        layout.addSpacing(12)

        self.searchField = qt.QLineEdit()
        self.searchField.setObjectName("StudioWhereSearch")
        self.searchField.setPlaceholderText(_("Search: add data, layout, python, window level…"))
        self.searchField.setClearButtonEnabled(True)
        self.searchField.setMinimumHeight(30)
        self.searchField.connect("textChanged(QString)", self._filter)
        layout.addWidget(self.searchField)
        layout.addSpacing(8)

        self.scrollArea = qt.QScrollArea()
        self.scrollArea.setObjectName("StudioWhereScroll")
        self.scrollArea.setFrameShape(qt.QFrame.NoFrame)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setHorizontalScrollBarPolicy(qt.Qt.ScrollBarAlwaysOff)
        listWidget = qt.QWidget()
        listWidget.setObjectName("StudioWhereList")
        self.listLayout = qt.QVBoxLayout(listWidget)
        self.listLayout.setContentsMargins(0, 0, 6, 0)
        self.listLayout.setSpacing(0)
        self.rowWidgets = {}
        for row in _migration().rows():
            widget = self._rowWidget(row)
            self.rowWidgets[row.id] = widget
            self.listLayout.addWidget(widget)
        self.emptyLabel = _label("", "StudioWhereEmpty", 13)
        self.emptyLabel.setContentsMargins(4, 12, 4, 12)
        self.emptyLabel.hide()
        self.listLayout.addWidget(self.emptyLabel)
        self.listLayout.addStretch(1)
        self.scrollArea.setWidget(listWidget)
        layout.addWidget(self.scrollArea, 1)
        layout.addSpacing(10)

        footer = qt.QHBoxLayout()
        footer.setSpacing(4)
        self.tourLink = self._link(_("Take the 'Coming from 3D Slicer' tour"), "StudioWhereTourLink", self._takeTour)
        footer.addWidget(self.tourLink)
        footer.addStretch(1)
        self.classicLink = self._link(_("Classic interface"), "StudioWhereClassicLink", self._openClassic)
        self.classicLink.setToolTip(ui.tooltip(_("Classic interface"), "Ctrl+Alt+0", _("Everything exactly as in 3D Slicer")))
        footer.addWidget(self.classicLink)
        layout.addLayout(footer)

        self._keys = _SearchKeys(self)
        self.searchField.installEventFilter(self._keys)
        self._watcher = _PanelWatcher(self)
        mainWindow.installEventFilter(self._watcher)
        ui.bindStyleSheet(self.frame, self._styleSheet)
        self.refreshIcons()
        ui.addPaletteListener(self.refreshIcons)
        self.frame.hide()

    @staticmethod
    def _link(text, objectName, callback):
        link = qt.QPushButton(text)
        link.setObjectName(objectName)
        link.setProperty("studioWhereLink", True)
        link.setFlat(True)
        link.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        _font(link, 12.5)
        link.connect("clicked()", callback)
        return link

    def _rowWidget(self, row):
        widget = qt.QFrame()
        widget.setObjectName("StudioWhereRow")
        widget.setProperty("rowId", row.id)
        layout = qt.QVBoxLayout(widget)
        layout.setContentsMargins(4, 10, 4, 10)
        layout.setSpacing(3)
        official = _label(row.officialText(), "StudioWhereOfficial", 13, qt.QFont.DemiBold)
        layout.addWidget(official)
        studio = _label("→ " + row.studioText(), "StudioWhereStudio", 12.5)
        layout.addWidget(studio)
        if row.note:
            note = _label(_migration().formatKeys(row.note), "StudioWhereNote", 12)
            layout.addWidget(note)
        actions = qt.QHBoxLayout()
        actions.setContentsMargins(0, 3, 0, 0)
        actions.setSpacing(6)
        if row.same:
            actions.addWidget(self._chip(_("Same as 3D Slicer"), "ok"))
        if row.classic:
            actions.addWidget(self._chip(_("Classic only"), "neutral"))
        if row.planned:
            actions.addWidget(self._chip(_("Coming in a later version"), "neutral"))
        actions.addStretch(1)
        if row.target and not row.planned:
            showMe = qt.QPushButton(_("Show me"))
            showMe.setObjectName("StudioWhereShowMeButton")
            showMe.setProperty("rowId", row.id)
            ui.setRole(showMe, "ghost")
            _font(showMe, 12.5)
            showMe.setToolTip(_("Point at it in Slicer Studio"))
            showMe.connect("clicked()", lambda rowId=row.id: self.plugin.showMe(rowId))
            actions.addWidget(showMe)
        if row.classic:
            openClassic = qt.QPushButton(_("Open Classic"))
            openClassic.setObjectName("StudioWhereOpenClassicButton")
            ui.setRole(openClassic, "ghost")
            _font(openClassic, 12.5)
            openClassic.connect("clicked()", self._openClassic)
            actions.addWidget(openClassic)
        layout.addLayout(actions)
        return widget

    @staticmethod
    def _chip(text, level):
        chip = ui.chip(text, level)
        chip.setObjectName("StudioWhereChip")
        return chip

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("card", "border", "hairline", "text", "text2", "text3", "accent-fg", "base", "control-border")}
        return (
            f"QFrame#StudioWhereMap {{ background-color: {t['card']}; border: 1px solid {t['border']}; border-radius: 12px; }}"
            "QScrollArea#StudioWhereScroll, QWidget#StudioWhereList { background: transparent; border: none; }"
            "QScrollArea#StudioWhereScroll > QWidget#qt_scrollarea_viewport { background: transparent; }"
            f"QFrame#StudioWhereRow {{ background: transparent; border: none; border-bottom: 1px solid {t['hairline']}; }}"
            f"QLabel#StudioWhereTitle, QLabel#StudioWhereOfficial {{ color: {t['text']}; background: transparent; border: none; }}"
            f"QLabel#StudioWhereSubtitle, QLabel#StudioWhereStudio, QLabel#StudioWhereEmpty {{ color: {t['text2']}; background: transparent; border: none; }}"
            f"QLabel#StudioWhereNote {{ color: {t['text3']}; background: transparent; border: none; }}"
            f"QLineEdit#StudioWhereSearch {{ background-color: {t['base']}; color: {t['text']}; border: 1px solid {t['control-border']}; border-radius: 6px; padding: 2px 8px; }}"
            f"QLineEdit#StudioWhereSearch:focus {{ border: 2px solid {t['accent-fg']}; padding: 1px 7px; }}"
            f"QPushButton[studioWhereLink=\"true\"] {{ background: transparent; border: none; color: {t['accent-fg']}; padding: 2px 4px; }}"
            "QPushButton[studioWhereLink=\"true\"]:hover { text-decoration: underline; }")

    def refreshIcons(self):
        if not _isAlive(self.frame):
            return
        self.icon.setPixmap(ui.pixmap("signpost", 18, "accent-fg"))
        self.closeButton.setIcon(ui.icon("x", 16))

    def _filter(self, text=None):
        text = self.searchField.text if text is None else text
        matches = _migration().lookup(text)
        matchIds = [row.id for row in matches]
        for widget in self.rowWidgets.values():
            self.listLayout.removeWidget(widget)
        for rowId, widget in self.rowWidgets.items():
            widget.setVisible(rowId in matchIds)
        for position, rowId in enumerate(matchIds):
            self.listLayout.insertWidget(position, self.rowWidgets[rowId])
        if not matchIds:
            self.emptyLabel.setText(_("No match for “{text}”. Try “save”, “layout”, “python” or “window level”, "
                                      "or open the Classic interface.").format(text=text.strip()))
        self.emptyLabel.setVisible(not matchIds)
        self.scrollArea.verticalScrollBar().setValue(0)

    def visibleRowIds(self):
        ids = []
        for index in range(self.listLayout.count()):
            widget = self.listLayout.itemAt(index).widget()
            if widget is not None and widget.objectName == "StudioWhereRow" and not widget.isHidden():
                ids.append(widget.property("rowId"))
        return ids

    def updatePosition(self):
        mainWindow = self.plugin.shell.mainWindow
        width = min(ui.fontPx(MAP_PANEL_WIDTH), mainWindow.width - 2 * MAP_PANEL_MARGIN)
        height = max(200, mainWindow.height - MAP_PANEL_TOP - MAP_PANEL_MARGIN)
        self.frame.setGeometry(mainWindow.width - width - MAP_PANEL_MARGIN, MAP_PANEL_TOP, width, height)
        self.frame.raise_()

    def open(self, query=None):
        if query is not None:
            self.searchField.setText(query)
        self._filter()
        self.updatePosition()
        self.frame.show()
        self.frame.raise_()
        self.plugin.shell.registerEscapeOverlay(self.frame, self.close)
        self.searchField.setFocus(qt.Qt.OtherFocusReason)

    def close(self):
        self.frame.hide()
        self.plugin.shell.unregisterEscapeOverlay(self.frame)

    def isVisible(self):
        return _isAlive(self.frame) and self.frame.isVisible()

    def _takeTour(self):
        self.close()
        self.plugin.startTour("from-slicer")

    def _openClassic(self):
        self.close()
        self.plugin.shell.classic()

    def dispose(self):
        ui.removePaletteListener(self.refreshIcons)
        ui.bindStyleSheet(self.frame, None)
        try:
            self.plugin.shell.unregisterEscapeOverlay(self.frame)
        except Exception:
            pass
        self.searchField.removeEventFilter(self._keys)
        self.plugin.shell.mainWindow.removeEventFilter(self._watcher)
        self.frame.hide()
        self.frame.setObjectName("")
        self.frame.deleteLater()


class _SearchKeys(qt.QObject):
    """Esc in the search field of the map: clear the text, then close the panel."""

    def __init__(self, panel):
        qt.QObject.__init__(self)
        self.panel = panel

    def eventFilter(self, obj, event):
        if event.type() not in (qt.QEvent.ShortcutOverride, qt.QEvent.KeyPress) or event.key() != qt.Qt.Key_Escape:
            return False
        if event.type() == qt.QEvent.ShortcutOverride:
            event.accept()
            return True
        if self.panel.searchField.text:
            self.panel.searchField.clear()
        else:
            self.panel.close()
        return True


class _PanelWatcher(qt.QObject):
    def __init__(self, panel):
        qt.QObject.__init__(self)
        self.panel = panel

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize and self.panel.isVisible():
            self.panel.updatePosition()
        return False


#
# First-start offer
#


class _OfferCard:
    """The non-modal "Take a 2-minute tour" card on the Library canvas (bottom right, away from the page column)."""

    def __init__(self, plugin, canvas):
        self.plugin = plugin
        self.canvas = canvas
        self.frame = qt.QFrame(canvas)
        self.frame.setObjectName("StudioTourOffer")
        layout = qt.QVBoxLayout(self.frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(0)
        header = qt.QHBoxLayout()
        header.setSpacing(8)
        self.icon = qt.QLabel()
        self.icon.setObjectName("StudioTourOfferIcon")
        header.addWidget(self.icon, 0, qt.Qt.AlignVCenter)
        title = _label(_("New to Slicer Studio?"), "StudioTourOfferTitle", 14, qt.QFont.DemiBold, wrap=False)
        header.addWidget(title, 1)
        layout.addLayout(header)
        layout.addSpacing(6)
        text = _label(_("Take a 2-minute tour of the workspaces, Search and the Classic interface, with how each is done in 3D Slicer."),
                      "StudioTourOfferText", 12.5)
        layout.addWidget(text)
        layout.addSpacing(12)
        buttons = qt.QHBoxLayout()
        buttons.setSpacing(6)
        self.takeButton = qt.QPushButton(_("Take the tour"))
        self.takeButton.setObjectName("StudioTourOfferTakeButton")
        ui.setRole(self.takeButton, "primary")
        self.takeButton.setMinimumHeight(30)
        self.takeButton.connect("clicked()", plugin._onOfferTake)
        buttons.addWidget(self.takeButton)
        buttons.addStretch(1)
        self.notNowButton = qt.QPushButton(_("Not now"))
        self.notNowButton.setObjectName("StudioTourOfferNotNowButton")
        ui.setRole(self.notNowButton, "ghost")
        self.notNowButton.connect("clicked()", plugin._onOfferNotNow)
        buttons.addWidget(self.notNowButton)
        layout.addLayout(buttons)
        layout.addSpacing(8)
        self.movedLink = qt.QPushButton(_("Coming from 3D Slicer? See what moved"))
        self.movedLink.setObjectName("StudioTourOfferMovedLink")
        self.movedLink.setFlat(True)
        self.movedLink.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.movedLink.connect("clicked()", plugin._onOfferMoved)
        layout.addWidget(self.movedLink, 0, qt.Qt.AlignLeft)
        for button in (self.takeButton, self.notNowButton, self.movedLink):
            # Reachable with Tab, but never takes the focus by itself: ignoring the card costs nothing
            button.setFocusPolicy(qt.Qt.TabFocus)
            button.setAutoDefault(False)
            _font(button, 12.5, qt.QFont.DemiBold if button is self.takeButton else None)
        ui.bindStyleSheet(self.frame, self._styleSheet)
        self.refreshIcons()
        ui.addPaletteListener(self.refreshIcons)
        self._watcher = _OfferWatcher(self)
        canvas.installEventFilter(self._watcher)
        #: Library page widgets watched for content changes (see _watchPage())
        self._watchedPage = []
        self.frame.setFixedWidth(ui.fontPx(OFFER_CARD_WIDTH))
        self._fitHeight()
        self.updatePosition()
        self.frame.show()
        self.frame.raise_()

    @staticmethod
    def _styleSheet():
        t = {name: ui.token(name).name() for name in ("card", "border", "text", "text2", "accent-fg")}
        return (
            f"QFrame#StudioTourOffer {{ background-color: {t['card']}; border: 1px solid {t['border']}; border-radius: 12px; }}"
            f"QLabel#StudioTourOfferTitle {{ color: {t['text']}; background: transparent; border: none; }}"
            f"QLabel#StudioTourOfferText {{ color: {t['text2']}; background: transparent; border: none; }}"
            "QLabel#StudioTourOfferIcon { background: transparent; border: none; }"
            f"QPushButton#StudioTourOfferMovedLink {{ background: transparent; border: none; color: {t['accent-fg']}; padding: 2px 0px; text-align: left; }}"
            "QPushButton#StudioTourOfferMovedLink:hover { text-decoration: underline; }")

    def refreshIcons(self):
        if _isAlive(self.frame):
            self.icon.setPixmap(ui.pixmap("compass", 18, "accent-fg"))

    def _fitHeight(self):
        """Word-wrapped texts: the height for the fixed width."""
        self.frame.ensurePolished()
        layout = self.frame.layout()
        layout.invalidate()
        layout.activate()
        width = self.frame.width
        height = layout.totalHeightForWidth(width) if layout.hasHeightForWidth() else layout.totalSizeHint().height()
        self.frame.setFixedHeight(height)

    def updatePosition(self):
        """Bottom right of the canvas, away from what the Library page shows; always inside the canvas.

        The page column is up to 1100 px wide (VISUAL 4.2) and mostly empty on its right side, so the card is kept off
        what the column paints (``_pageContentItems()``), not off the column box. When the bottom right corner covers
        it (a folder card, projects), the card goes below the content, to the top right or to the bottom left corner,
        whichever covers the least (a button or a field counts ten times its area); it never leaves the canvas.
        """
        if not _isAlive(self.canvas) or not _isAlive(self.frame):
            return
        width, height = self.frame.width, self.frame.height
        rect = self.canvas.rect
        margin = 24
        right = rect.width() - width - margin
        bottom = rect.height() - height - margin
        candidates = [(right, bottom)]
        column = self.canvas.findChild("QWidget", "StudioLibraryColumn")
        content = []
        if column is not None and column.isVisible():
            self._watchPage(column)
            content = _pageContentItems(column, self.canvas)
        if content:
            below = max(contentRect.bottom() for contentRect, _interactive in content) + margin
            columnRect = _rectIn(column, self.canvas)
            candidates += [(max(margin, columnRect.center().x() - width // 2), below), (right, margin), (margin, bottom)]

        def covered(position):
            cardRect = qt.QRect(position[0], position[1], width, height)
            if not rect.contains(cardRect):
                return float("inf")
            area = 0
            for contentRect, interactive in content:
                overlap = cardRect.intersected(contentRect)
                if not overlap.isEmpty():
                    area += overlap.width() * overlap.height() * (10 if interactive else 1)
            return area

        x, y = min(candidates, key=covered)
        x = min(max(0, x), max(0, rect.width() - width))
        y = min(max(0, y), max(0, rect.height() - height))
        position = self.frame.pos
        if (position.x(), position.y()) != (int(x), int(y)):
            self.frame.move(int(x), int(y))
        self.frame.raise_()

    def _watchPage(self, column):
        """Place the card again when the page content changes (a folder card, projects) or scrolls."""
        for widget in (column, column.parentWidget()):
            if widget is not None and not any(widget == watched for watched in self._watchedPage):
                widget.installEventFilter(self._watcher)
                self._watchedPage.append(widget)

    def dispose(self):
        ui.removePaletteListener(self.refreshIcons)
        ui.bindStyleSheet(self.frame, None)
        if _isAlive(self.canvas):
            self.canvas.removeEventFilter(self._watcher)
        for widget in self._watchedPage:
            if _isAlive(widget):
                widget.removeEventFilter(self._watcher)
        self._watchedPage = []
        if _isAlive(self.frame):
            self.frame.hide()
            self.frame.setObjectName("")
            self.frame.deleteLater()


class _OfferWatcher(qt.QObject):
    def __init__(self, card):
        qt.QObject.__init__(self)
        self.card = card

    def eventFilter(self, obj, event):
        if event.type() in (qt.QEvent.Resize, qt.QEvent.Show, qt.QEvent.LayoutRequest, qt.QEvent.Move):
            # The layout of the canvas is applied before its event filters run: place the card now, so that it is
            # painted in the same frame as the canvas (a move after it costs one more repaint of the window), and
            # again when the events queued by the change are processed (no repaint if it does not move then)
            self.card.updatePosition()
            qt.QTimer.singleShot(0, self.card.updatePosition)
        return False


#
# The plug-in
#


class TourPlugin:
    """Menus, help link, first-start offer, the tour player and the map panel of one shell."""

    OFFER_NOTICE_KEY = "StudioTourOffer"
    HELP_LINK_KEY = "tour-workspace"

    def __init__(self, shell, offer=True):
        self.shell = shell
        self.player = None
        self.whereMap = None
        self.offerCard = None
        self.offerNotice = None
        self._offerTimer = None
        self._offerRetries = 0
        self._sceneObservations = []
        self._helpMenu = shell.mainWindow.findChild("QMenu", "HelpMenu")
        self.actions = self._createActions()
        appMenu = getattr(getattr(shell, "topBar", None), "appMenu", None)
        if appMenu is not None and hasattr(appMenu, "addHelpActions"):
            appMenu.addHelpActions(self.actions)
        if self._helpMenu is not None:
            self._helpMenu.connect("aboutToShow()", self._updateActions)
        if hasattr(shell, "addHelpLink"):
            shell.addHelpLink(self.HELP_LINK_KEY, _("Tour this workspace"), self.startWorkspaceTour, self.hasWorkspaceTour)
        if offer and not isOffered() and not slicer.app.testingEnabled():
            self.scheduleOffer()

    def _createActions(self):
        mainWindow = self.shell.mainWindow
        specs = (
            ("StudioTakeTourAction", _("Take a tour…"), "compass", lambda: self.startTour("studio")),
            ("StudioTourWorkspaceAction", _("Tour this workspace…"), None, self.startWorkspaceTour),
            ("StudioComingFromSlicerAction", _("Coming from 3D Slicer…"), None, lambda: self.startTour("from-slicer")),
            ("StudioWhereDidItGoAction", _("Where did it go? (3D Slicer → Studio)"), "signpost", self.showMigrationMap),
        )
        actions = []
        for objectName, text, iconName, callback in specs:
            action = qt.QAction(text, mainWindow)
            action.setObjectName(objectName)
            if iconName:
                action.setProperty("studioIcon", iconName)
                action.setIcon(ui.icon(iconName, 16))
            action.connect("triggered()", callback)
            actions.append(action)
        return actions

    def _updateActions(self):
        for action in self.actions:
            if action.objectName == "StudioTourWorkspaceAction":
                action.setEnabled(self.hasWorkspaceTour())
                action.setText(self.workspaceTourText())
            if action.property("studioIcon"):
                action.setIcon(ui.icon(action.property("studioIcon"), 16))

    #
    # Tours
    #

    def _workspaceTour(self):
        key = self.shell.currentWorkspace() or self.shell.transientOrigin()
        tour = _tours().workspaceTour(key) if key else None
        return tour if tour is not None and tour.isAvailable(self.shell) else None

    def hasWorkspaceTour(self):
        return self._workspaceTour() is not None

    def workspaceTourText(self):
        """Text of the ≡ > Help item: "Tour Library…" (the workspace's title), or "Tour this workspace…"."""
        from . import workspaces

        tour = self._workspaceTour()
        ws = workspaces.workspace(tour.workspace) if tour is not None else None
        return _("Tour {workspace}…").format(workspace=ws.title) if ws is not None else _("Tour this workspace…")

    def availableTours(self):
        return [tour for tour in _tours().TOURS if tour.isAvailable(self.shell)]

    def startTour(self, tourId="studio"):
        if not self.shell.isActive() or _modalOpen():
            return False
        tour = _tours().tour(tourId)
        if tour is None or not tour.isAvailable(self.shell):
            return False
        self.stopTour()
        self._dismissOffer(markOffered=True)
        if self.whereMap is not None and self.whereMap.isVisible():
            self.whereMap.close()
        onFinished = self._afterFromSlicer if tourId == "from-slicer" else None
        player = TourPlayer(self.shell, tour, onFinished=onFinished)
        self.player = player
        if not player.start():
            self.player = None
            return False
        return True

    def startWorkspaceTour(self):
        tour = self._workspaceTour()
        if tour is None:
            return self.startTour("studio")
        # Every step of a workspace tour shows its workspace itself, after the tour recorded the view (a module opened
        # from Search comes back when the tour ends)
        return self.startTour(tour.id)

    def _afterFromSlicer(self, player):
        if player.completed:
            self.showMigrationMap()

    def stopTour(self):
        if self.player is not None:
            player = self.player
            self.player = None
            player.stop()

    def activeTour(self):
        return self.player.tour.id if self.player is not None and self.player.active else None

    #
    # Where did it go?
    #

    def showMigrationMap(self, query=None):
        if not self.shell.isActive():
            return False
        if self.player is not None and self.player.active:
            self.stopTour()
        if self.whereMap is None:
            self.whereMap = WhereMap(self)
        self.whereMap.open(query)
        return True

    def showMe(self, rowId):
        """Spotlight the Studio control of a 'Where did it go?' row; back to the map afterwards."""
        tours = _tours()
        row = _migration().row(rowId)
        if row is None or not row.target or row.planned or not self.shell.isActive() or _modalOpen():
            return False
        body = row.studioText()
        if row.note:
            body = body.rstrip(".") + ". " + _migration().formatKeys(row.note)
        step = tours.Step(row.id, row.target, _("{place} is here").format(place=row.official), body, rows=(row.id,), workspace=row.workspace,
                          inspector=row.workspace == "draw", placement="auto")
        fromMap = self.whereMap is not None and self.whereMap.isVisible()
        query = self.whereMap.searchField.text if fromMap else None
        if fromMap:
            self.whereMap.close()
        self.stopTour()

        def onFinished(player):
            if fromMap:
                self.showMigrationMap(query)

        player = TourPlayer(self.shell, _SingleStepTour(step, row.official), onFinished=onFinished, single=True,
                            finishText=_("Back to the map") if fromMap else _("Done"))
        self.player = player
        if not player.start():
            self.player = None
            self.shell.toast(_("{place} is not shown right now").format(place=row.official))
            if fromMap:
                self.showMigrationMap(query)
            return False
        return True

    #
    # First-start offer
    #

    def scheduleOffer(self):
        self._offerRetries = 0
        if self._offerTimer is None:
            self._offerTimer = qt.QTimer()
            self._offerTimer.setSingleShot(True)
            self._offerTimer.connect("timeout()", self._tryOffer)
        self._offerTimer.start(OFFER_DELAY_MS)

    def _otherPromptShown(self):
        """True while another first-run or recovery prompt is shown (or a dialog, a popup or a tour)."""
        if _modalOpen() or slicer.app.activePopupWidget() is not None:
            return True
        if self.player is not None and self.player.active:
            return True
        notice = self.shell.inspectorNotice()
        if notice is not None and _isAlive(notice) and (notice.isVisible() or self.shell.currentWorkspace() != "library"):
            # Outside Library the offer is an inspector notice, which would replace this one (also one registered
            # for another workspace, which is hidden now)
            return True
        for widget in slicer.app.topLevelWidgets():
            if widget.objectName in ("StudioRecoveryDialog",) and widget.isVisible():
                return True
        card = self.shell.mainWindow.findChild("QWidget", "StudioRecoveryCard")
        return card is not None and card.isVisible()

    def _tryOffer(self):
        if not self.shell.isActive() or isOffered() or self.offerCard is not None or self.offerNotice is not None:
            return
        if self._otherPromptShown() or not self.shell.mainWindow.isVisible():
            self._offerRetries += 1
            if self._offerRetries < OFFER_MAX_RETRIES and self._offerTimer is not None:
                self._offerTimer.start(OFFER_RETRY_MS)
            return
        self.offer()

    def offer(self):
        """Show the first-start offer: a card on the Library canvas, or a notice line in the inspector elsewhere."""
        if self.offerCard is not None or self.offerNotice is not None or not self.shell.isActive():
            return False
        canvas = self.shell.canvas() if self.shell.currentWorkspace() == "library" else None
        if canvas is not None and _isAlive(canvas):
            self.offerCard = _OfferCard(self, canvas)
        else:
            notice = self.shell.inspectorNotice()
            if notice is not None and _isAlive(notice):
                # The slot holds one notice: never replace another one
                return False
            self.offerNotice = ui.noticeLine(_("New to Slicer Studio? Take a 2-minute tour."), [
                (_("Take the tour"), self._onOfferTake), (_("What moved"), self._onOfferMoved), (_("Not now"), self._onOfferNotNow)])
            self.offerNotice.setObjectName("StudioTourOfferNotice")
            self.shell.setInspectorNotice(self.offerNotice, self.OFFER_NOTICE_KEY)
        # Opening a file counts as an answer: the offer goes away and is not shown again
        scene = slicer.mrmlScene
        for event in (scene.NodeAddedEvent, scene.EndImportEvent):
            self._sceneObservations.append(scene.AddObserver(event, self._onSceneChanged))
        return True

    def offerWidget(self):
        if self.offerCard is not None and _isAlive(self.offerCard.frame):
            return self.offerCard.frame
        return self.offerNotice

    def _onSceneChanged(self, caller=None, event=None):
        if self.player is not None and self.player.active:
            # Showing Draw during a tour may create a segmentation: not the user's file
            return
        qt.QTimer.singleShot(0, self._checkDataOpened)

    def _checkDataOpened(self):
        from . import library

        if library.hasData():
            self._dismissOffer(markOffered=True)

    def _dismissOffer(self, markOffered):
        if markOffered and (self.offerCard is not None or self.offerNotice is not None):
            _markOffered()
        if self._offerTimer is not None:
            self._offerTimer.stop()
        for observation in self._sceneObservations:
            slicer.mrmlScene.RemoveObserver(observation)
        self._sceneObservations = []
        if self.offerCard is not None:
            self.offerCard.dispose()
            self.offerCard = None
        if self.offerNotice is not None:
            self.shell.clearInspectorNotice(self.OFFER_NOTICE_KEY)
            if _isAlive(self.offerNotice):
                self.offerNotice.hide()
                self.offerNotice.deleteLater()
            self.offerNotice = None

    def _onOfferTake(self):
        self._dismissOffer(markOffered=True)
        qt.QTimer.singleShot(0, lambda: self.startTour("studio"))

    def _onOfferMoved(self):
        self._dismissOffer(markOffered=True)
        qt.QTimer.singleShot(0, lambda: self.startTour("from-slicer"))

    def _onOfferNotNow(self):
        self._dismissOffer(markOffered=True)

    #
    # Uninstall
    #

    def dispose(self):
        if self.player is not None:
            player = self.player
            self.player = None
            player.abandon()
        self._dismissOffer(markOffered=False)
        self._offerTimer = None
        if self.whereMap is not None:
            self.whereMap.dispose()
            self.whereMap = None
        if self._helpMenu is not None:
            self._helpMenu.disconnect("aboutToShow()", self._updateActions)
        appMenu = getattr(getattr(self.shell, "topBar", None), "appMenu", None)
        if appMenu is not None and hasattr(appMenu, "removeHelpActions"):
            appMenu.removeHelpActions(self.actions)
        elif self._helpMenu is not None:
            for action in self.actions:
                self._helpMenu.removeAction(action)
        if hasattr(self.shell, "removeHelpLink"):
            self.shell.removeHelpLink(self.HELP_LINK_KEY)
        for action in self.actions:
            action.setObjectName("")
            action.deleteLater()
        self.actions = []


#
# Plug-in protocol and API
#


def install(shell):
    """Studio plug-in entry point (``shell.OPTIONAL_PLUGINS``)."""
    global _plugin
    startTime = time.perf_counter()
    if _plugin is not None:
        _plugin.dispose()
    _plugin = TourPlugin(shell)
    _plugin.installDurationMs = (time.perf_counter() - startTime) * 1000.0


def uninstall(shell):
    """Remove everything (the shell goes Classic): a running tour is closed without showing workspaces again."""
    global _plugin
    if _plugin is not None:
        _plugin.dispose()
        _plugin = None


def plugin():
    """The live plug-in, or None (Classic, testing without a shell, no main window)."""
    if _plugin is not None and _plugin.shell.isActive():
        return _plugin
    return None


def startTour(tourId="studio"):
    """Start a tour ("studio", "from-slicer", "library", "draw", "map"). Return True if it started."""
    live = plugin()
    return live.startTour(tourId) if live is not None else False


def startWorkspaceTour():
    """Start the tour of the current workspace (or the Studio tour). Return True if it started."""
    live = plugin()
    return live.startWorkspaceTour() if live is not None else False


def stopTour():
    """Leave the running tour, if any (everything is put back as it was)."""
    live = plugin()
    if live is not None:
        live.stopTour()


def activeTour():
    """Id of the running tour, or None."""
    live = plugin()
    return live.activeTour() if live is not None else None


def activePlayer():
    """The running ``TourPlayer`` (tests), or None."""
    live = plugin()
    return live.player if live is not None and live.player is not None and live.player.active else None


def availableTours():
    """The tours that can be started now: ``[{"id", "title", "description", "workspace"}]``."""
    live = plugin()
    if live is None:
        return []
    return [{"id": tour.id, "title": tour.title, "description": tour.description, "workspace": tour.workspace} for tour in live.availableTours()]


def showMigrationMap(query=None):
    """Open the 'Where did it go?' panel, optionally with a search. Return True if it is shown."""
    live = plugin()
    return live.showMigrationMap(query) if live is not None else False


def showMe(rowId):
    """Point at the Studio control of a 'Where did it go?' row (``migration.row(rowId)``). Return True if shown."""
    live = plugin()
    return live.showMe(rowId) if live is not None else False


def offerTour():
    """Show the first-start offer now (tests; it is shown once by itself on the first Studio start)."""
    live = plugin()
    return live.offer() if live is not None else False

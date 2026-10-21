"""Heads-up display of Slicer Studio over the view area (SPEC 2.4): short toast messages and the
floating "Exit focus mode" pill.
"""

import logging
import traceback

import qt
import slicer

from . import ui

TOAST_HEIGHT = 32
TOAST_BOTTOM_MARGIN = 28
FADE_DURATION_MS = 250
PILL_HEIGHT = 28
PILL_MARGIN = 12
#: Below the controller bar of the slice views (about 30 px), so that its controls stay reachable
PILL_TOP_MARGIN = 40
#: Height of the bottom corner text of a view (two lines: the laterality cue and the layer names), kept clear by
#: the toasts while they are beside a figure, where they reach the corner of the drawing view
CORNER_TEXT_RESERVE = 44


def _mapRect(widget, parentWidget):
    """The geometry of ``widget`` in the coordinates of ``parentWidget`` (an ancestor or not)."""
    topLeft = parentWidget.mapFromGlobal(widget.mapToGlobal(qt.QPoint(0, 0)))
    return qt.QRect(topLeft.x(), topLeft.y(), widget.width, widget.height)


def viewRects(parentWidget):
    """The visible views of the layout: [(name, QRect in ``parentWidget`` coordinates)], slice views first.

    ``name`` is the slice view name ("Red") or the 3D view node's singleton tag ("1").
    """
    layoutManager = slicer.app.layoutManager()
    if layoutManager is None or parentWidget is None:
        return []
    rects = []
    try:
        for name in layoutManager.sliceViewNames():
            widget = layoutManager.sliceWidget(name)
            if widget is not None and widget.isVisible() and widget.width > 0 and widget.height > 0:
                rects.append((str(name), _mapRect(widget, parentWidget)))
        for index in range(layoutManager.threeDViewCount):
            widget = layoutManager.threeDWidget(index)
            if widget is not None and widget.isVisible() and widget.width > 0 and widget.height > 0:
                viewNode = widget.mrmlViewNode() if hasattr(widget, "mrmlViewNode") else None
                name = viewNode.GetSingletonTag() if viewNode is not None else str(index)
                rects.append((str(name), _mapRect(widget, parentWidget)))
    except Exception:
        logging.error(f"Studio: cannot list the views:\n{traceback.format_exc()}")
        return []
    return rects


def figureViewName():
    """The name of the slice view that shows a figure (Compare layout), None when no figure is shown."""
    try:
        from . import importer
        controller = importer.figureController()
    except Exception:
        return None
    return getattr(importer, "FIGURE_VIEW_NAME", None) if controller is not None else None


def _figureSpan(parentWidget):
    """The free span (left, width) beside the figure view, None when no figure is shown or it leaves no room."""
    width = parentWidget.width
    figureName = figureViewName()
    if figureName is None:
        return None
    figureRect = dict(viewRects(parentWidget)).get(figureName)
    if figureRect is None:
        return None
    left, right = max(0, figureRect.left()), min(width, figureRect.right() + 1)
    # The larger free part beside the figure
    if left >= width - right:
        span = (0, left)
    else:
        span = (right, width - right)
    return span if span[1] >= 160 else None


def toastSpan(parentWidget):
    """The horizontal span (left, width) in which a toast is centred.

    It is the whole width of ``parentWidget``, except while a figure is shown beside the views (Compare layout):
    the toast is then centred beside the figure view, so that it never covers the figure or its tag.
    """
    span = _figureSpan(parentWidget)
    return span if span is not None else (0, parentWidget.width)


def toastGeometry(parentWidget, preferredWidth):
    """The geometry (x, y, width) of a toast of ``preferredWidth`` near the bottom of ``parentWidget``.

    Beside a figure the toast is also raised above the bottom corner text of the drawing view, so that the
    laterality cue stays visible (USABILITY P6).
    """
    span = _figureSpan(parentWidget)
    left, spanWidth = span if span is not None else (0, parentWidget.width)
    width = min(preferredWidth, max(120, spanWidth - 48))
    y = parentWidget.height - TOAST_HEIGHT - TOAST_BOTTOM_MARGIN
    if span is not None:
        y -= CORNER_TEXT_RESERVE
    return left + (spanWidth - width) // 2, max(0, y), width


def elidedText(label, text, overflow):
    """``text`` shortened with an ellipsis so that ``label`` gets ``overflow`` pixels narrower (``text`` if <= 0)."""
    if overflow <= 0 or not text:
        return text
    metrics = label.fontMetrics()
    advance = getattr(metrics, "horizontalAdvance", None)
    textWidth = advance(text) if advance is not None else metrics.width(text)
    return metrics.elidedText(text, qt.Qt.ElideRight, max(20, textWidth - overflow))


class Toast:
    """A 32 px pill near the bottom of a widget (the main window central widget) that fades out.

    It is centred on the widget, or beside the figure view while a figure is shown (``toastSpan()``).

    The pill is transparent for mouse events and is raised above the Studio canvas.
    """

    def __init__(self, parentWidget):
        self.parentWidget = parentWidget
        self.fullText = ""
        self.label = qt.QLabel(parentWidget)
        self.label.setObjectName("StudioToast")
        self.label.setAlignment(qt.Qt.AlignCenter)
        self.label.setFixedHeight(TOAST_HEIGHT)
        self.label.setAttribute(qt.Qt.WA_TransparentForMouseEvents)
        self.label.hide()
        ui.bindStyleSheet(self.label, self._styleSheet)

        self.opacityEffect = qt.QGraphicsOpacityEffect(self.label)
        self.opacityEffect.setOpacity(1.0)
        self.label.setGraphicsEffect(self.opacityEffect)

        self.fadeAnimation = qt.QPropertyAnimation(self.opacityEffect, qt.QByteArray(b"opacity"), self.label)
        self.fadeAnimation.setDuration(FADE_DURATION_MS)
        self.fadeAnimation.setStartValue(1.0)
        self.fadeAnimation.setEndValue(0.0)
        self.fadeAnimation.connect("finished()", self.label.hide)

        self.hideTimer = qt.QTimer(self.label)
        self.hideTimer.setSingleShot(True)
        self.hideTimer.connect("timeout()", self.fadeAnimation.start)

    @staticmethod
    def _styleSheet():
        # Tooltip colors: an inverted pill that reads well over images and over the canvas
        return (
            f"QLabel#StudioToast {{ background-color: {ui._rgba(ui.token('tooltip'), 0.92)}; color: {ui.token('tooltip-text').name()};"
            f" border: 1px solid {ui._rgba(ui.token('tooltip-text'), 0.12)}; border-radius: {TOAST_HEIGHT // 2}px; padding: 0px 16px; font-size: {ui.fontPx(13)}px; }}")

    def show(self, text, ms=1600):
        """Show ``text`` for ``ms`` milliseconds, then fade out. A new message replaces the current one."""
        self.fadeAnimation.stop()
        self.opacityEffect.setOpacity(1.0)
        self.fullText = text
        self.label.setAccessibleName(text)
        self.label.setText(text)
        self.label.adjustSize()
        self.updatePosition()
        self.label.show()
        self.label.raise_()
        self.hideTimer.start(max(0, int(ms)))

    def updatePosition(self):
        if not self.fullText:
            return
        # The whole text, shortened with an ellipsis when the toast is narrower (beside a figure)
        self.label.setText(self.fullText)
        preferredWidth = self.label.sizeHint.width()
        x, y, width = toastGeometry(self.parentWidget, preferredWidth)
        self.label.setText(elidedText(self.label, self.fullText, preferredWidth - width))
        self.label.resize(width, TOAST_HEIGHT)
        self.label.move(x, y)

    def isVisible(self):
        return self.label.isVisible()

    def text(self):
        """The whole text (the label may show it shortened)."""
        return self.fullText

    def dispose(self):
        self.hideTimer.stop()
        self.fadeAnimation.stop()
        ui.bindStyleSheet(self.label, None)
        self.label.hide()
        self.label.deleteLater()


class CornerPill:
    """A small pill button floating in the top-right corner of a widget (the main window central widget).

    It is drawn at low emphasis (translucent tooltip colors) and becomes opaque when hovered. Studio uses
    it as the on-screen exit of the focus mode, where the top bar and the inspector are hidden.
    """

    def __init__(self, parentWidget, objectName, text, iconName, callback, toolTip=None):
        self.parentWidget = parentWidget
        self.iconName = iconName
        self.button = qt.QToolButton(parentWidget)
        self.button.setObjectName(objectName)
        self.button.setText(text)
        self.button.setToolTip(toolTip or text)
        self.button.setToolButtonStyle(qt.Qt.ToolButtonTextBesideIcon)
        self.button.setFocusPolicy(qt.Qt.NoFocus)
        self.button.setCursor(qt.QCursor(qt.Qt.PointingHandCursor))
        self.button.setIconSize(qt.QSize(14, 14))
        self.button.setFixedHeight(PILL_HEIGHT)
        self.button.connect("clicked()", callback)
        self.button.hide()
        ui.bindStyleSheet(self.button, self._styleSheet)
        self.refreshIcon()
        ui.addPaletteListener(self.refreshIcon)

    def _styleSheet(self):
        objectName = self.button.objectName
        background = ui.token("tooltip")
        text = ui.token("tooltip-text")
        return (
            f"QToolButton#{objectName} {{ background-color: {ui._rgba(background, 0.55)}; color: {ui._rgba(text, 0.8)};"
            f" border: 1px solid {ui._rgba(text, 0.12)}; border-radius: {PILL_HEIGHT // 2}px; padding: 0px 12px 0px 10px; font-size: {ui.fontPx(12)}px; }}"
            f"QToolButton#{objectName}:hover {{ background-color: {ui._rgba(background, 0.92)}; color: {text.name()}; }}"
            # Keyboard focus (USABILITY 9.2) if a pill is made focusable: a 2 px ring, the padding 1 px smaller (same size)
            f"QToolButton#{objectName}:focus {{ background-color: {ui._rgba(background, 0.92)}; color: {text.name()};"
            f" border: 2px solid {ui.token('accent-fg').name()}; padding: 0px 11px 0px 9px; }}")

    def refreshIcon(self):
        self.button.setIcon(qt.QIcon(ui.pixmap(self.iconName, 14, "tooltip-text")))

    def show(self):
        self.button.adjustSize()
        self.updatePosition()
        self.button.show()
        self.button.raise_()

    def hide(self):
        self.button.hide()

    def isVisible(self):
        return self.button.isVisible()

    def raise_(self):
        if self.button.isVisible():
            self.button.raise_()

    def updatePosition(self):
        self.button.move(self.parentWidget.width - self.button.width - PILL_MARGIN, PILL_TOP_MARGIN)

    def dispose(self):
        ui.removePaletteListener(self.refreshIcon)
        ui.bindStyleSheet(self.button, None)
        self.button.hide()
        self.button.setObjectName("")
        self.button.deleteLater()

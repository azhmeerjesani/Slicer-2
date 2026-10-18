"""Heads-up display of Slicer Studio over the view area (SPEC 2.4): short toast messages and the
floating "Exit focus mode" pill.
"""

import qt

from . import ui

TOAST_HEIGHT = 32
TOAST_BOTTOM_MARGIN = 28
FADE_DURATION_MS = 250
PILL_HEIGHT = 28
PILL_MARGIN = 12


class Toast:
    """A 32 px pill near the bottom of a widget (the main window central widget) that fades out.

    The pill is transparent for mouse events and is raised above the Studio canvas.
    """

    def __init__(self, parentWidget):
        self.parentWidget = parentWidget
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
            f" border: 1px solid {ui._rgba(ui.token('tooltip-text'), 0.12)}; border-radius: {TOAST_HEIGHT // 2}px; padding: 0px 16px; font-size: 13px; }}")

    def show(self, text, ms=1600):
        """Show ``text`` for ``ms`` milliseconds, then fade out. A new message replaces the current one."""
        self.fadeAnimation.stop()
        self.opacityEffect.setOpacity(1.0)
        self.label.setText(text)
        self.label.adjustSize()
        self.updatePosition()
        self.label.show()
        self.label.raise_()
        self.hideTimer.start(max(0, int(ms)))

    def updatePosition(self):
        if not self.label.text:
            return
        width = min(self.label.sizeHint.width(), max(120, self.parentWidget.width - 48))
        self.label.resize(width, TOAST_HEIGHT)
        self.label.move((self.parentWidget.width - width) // 2, self.parentWidget.height - TOAST_HEIGHT - TOAST_BOTTOM_MARGIN)

    def isVisible(self):
        return self.label.isVisible()

    def text(self):
        return self.label.text

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
            f" border: 1px solid {ui._rgba(text, 0.12)}; border-radius: {PILL_HEIGHT // 2}px; padding: 0px 12px 0px 10px; font-size: 12px; }}"
            f"QToolButton#{objectName}:hover {{ background-color: {ui._rgba(background, 0.92)}; color: {text.name()}; }}")

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
        self.button.move(self.parentWidget.width - self.button.width - PILL_MARGIN, PILL_MARGIN)

    def dispose(self):
        ui.removePaletteListener(self.refreshIcon)
        ui.bindStyleSheet(self.button, None)
        self.button.hide()
        self.button.deleteLater()

"""Heads-up display of Slicer Studio: short toast messages over the view area (SPEC 2.4)."""

import qt

from . import ui

TOAST_HEIGHT = 32
TOAST_BOTTOM_MARGIN = 28
FADE_DURATION_MS = 250


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

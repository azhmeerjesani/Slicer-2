"""Non-blocking downloads of Slicer Studio (SPEC 3.1, "Start on MNI template").

``Download`` fetches a file with ``qt.QNetworkAccessManager`` on the main thread event loop: the
application stays responsive while data arrives, and no MRML or VTK object is used outside the main
thread. The file is written next to its destination (``<dest>.part``), checked with its SHA-256 checksum
and only then renamed to its destination, so an interrupted or corrupted download never looks cached.

``DownloadBanner`` is the card shown over the view area that offers a download, shows its progress
with a Cancel button, and offers an action once the file is ready (for example "Switch to 1 mm").
"""

import hashlib
import logging
import os
import traceback

import qt
import slicer
from slicer.i18n import tr as _

from . import ui

#: Subdirectory of the application cache (``slicer.app.cachePath``) where Studio stores downloaded templates
CACHE_SUBDIRECTORY = "StudioTemplates"

#: Size of the chunks read when computing checksums
HASH_CHUNK_SIZE = 1024 * 1024

#: Abort a download when no data was received for this long (milliseconds)
TRANSFER_TIMEOUT_MS = 60000


def cacheDirectory():
    """Directory of the files downloaded by Studio (in the application cache)."""
    return os.path.join(slicer.app.cachePath, CACHE_SUBDIRECTORY)


def sha256(path):
    """Return the SHA-256 checksum (lowercase hexadecimal) of a file, read in 1 MB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalizedChecksum(checksum):
    """Return the hexadecimal digest of a SHA-256 checksum given as "<digest>" or "SHA256:<digest>"."""
    if not checksum:
        return None
    algorithm, separator, digest = checksum.rpartition(":")
    if separator and algorithm.upper() != "SHA256":
        raise ValueError(f"Unsupported checksum algorithm: {algorithm}")
    return digest.lower()


def _isAlive(qobject):
    """False if the C++ object was deleted."""
    try:
        qobject.objectName
    except Exception:
        return False
    return True


def formatSize(numberOfBytes):
    """Return a size in megabytes, for example "13.7 MB" (decimal units, as download sizes are usually shown)."""
    return _("{size} MB").format(size=f"{numberOfBytes / 1e6:.1f}")


class Signal:
    """Signal of a Python object: ``connect(callback)``, ``disconnect(callback)`` and ``emit(*args)``.

    Plain Python callbacks are used instead of ``qt.Signal``: a Python-defined Qt signal that is still
    connected when the application exits makes the PythonQt finalization crash.
    """

    def __init__(self):
        self._callbacks = []

    def connect(self, callback):
        if callback not in self._callbacks:
            self._callbacks.append(callback)

    def disconnect(self, callback=None):
        """Disconnect ``callback``, or every callback if it is None."""
        if callback is None:
            self._callbacks = []
        elif callback in self._callbacks:
            self._callbacks.remove(callback)

    def emit(self, *args):
        for callback in list(self._callbacks):
            try:
                callback(*args)
            except Exception:
                logging.error(f"Studio download callback failed:\n{traceback.format_exc()}")


class Download:
    """Download of ``url`` into the file ``dest``, verified with its SHA-256 checksum.

    Signals (see ``Signal``):

    - ``progress(bytesReceived, bytesTotal)``: ``bytesTotal`` is -1 while the size is unknown;
    - ``finished(path)``: the file is complete, its checksum matched and it is at ``dest``;
    - ``failed(message)``: the download failed, the checksum did not match or ``cancel()`` was called
      (then ``state`` is "canceled"). The partial file is removed and ``dest`` is not written.

    ``state`` is one of "idle", "running", "done", "failed" and "canceled".
    """

    _networkManager = None

    def __init__(self, url, sha256, dest):
        """``sha256`` is the expected checksum ("<digest>" or "SHA256:<digest>"), None to skip the check."""
        self.url = url
        self.sha256 = normalizedChecksum(sha256)
        self.dest = dest
        self.progress = Signal()
        self.finished = Signal()
        self.failed = Signal()
        self.state = "idle"
        self.bytesReceived = 0
        self.bytesTotal = -1
        self.errorMessage = ""
        self._reply = None
        self._file = None

    @classmethod
    def networkManager(cls):
        if cls._networkManager is None:
            cls._networkManager = qt.QNetworkAccessManager()
        return cls._networkManager

    @property
    def partPath(self):
        """File that receives the data until the download is complete and verified."""
        return self.dest + ".part"

    def isRunning(self):
        return self.state == "running"

    def start(self):
        """Start (or restart) the download. Signals are emitted from the application event loop."""
        if self.state == "running":
            return
        self.bytesReceived = 0
        self.bytesTotal = -1
        self.errorMessage = ""
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.dest)), exist_ok=True)
            self._file = open(self.partPath, "wb")
        except OSError as e:
            self._fail(_("Cannot write {path}: {reason}").format(path=self.partPath, reason=e.strerror or str(e)))
            return
        request = qt.QNetworkRequest(qt.QUrl(self.url))
        request.setAttribute(qt.QNetworkRequest.RedirectPolicyAttribute, qt.QNetworkRequest.NoLessSafeRedirectPolicy)
        request.setTransferTimeout(TRANSFER_TIMEOUT_MS)
        self.state = "running"
        reply = Download.networkManager().get(request)
        self._reply = reply
        reply.connect("readyRead()", self._onReadyRead)
        reply.connect("downloadProgress(qint64,qint64)", self._onDownloadProgress)
        reply.connect("finished()", self._onFinished)

    def cancel(self):
        """Abort the download. ``failed`` is emitted with ``state`` "canceled"; the partial file is removed."""
        if self.state != "running":
            return
        self.state = "canceled"
        self._releaseReply(abort=True)
        self._closeAndRemovePartFile()
        self.errorMessage = _("Download canceled")
        self.failed.emit(self.errorMessage)

    def _releaseReply(self, abort=False):
        reply = self._reply
        self._reply = None
        if reply is None:
            return
        for signal, slot in (("readyRead()", self._onReadyRead),
                             ("downloadProgress(qint64,qint64)", self._onDownloadProgress),
                             ("finished()", self._onFinished)):
            reply.disconnect(signal, slot)
        if abort:
            reply.abort()
        reply.deleteLater()

    def _closeAndRemovePartFile(self):
        if self._file is not None:
            self._file.close()
            self._file = None
        if os.path.exists(self.partPath):
            try:
                os.remove(self.partPath)
            except OSError as e:
                logging.warning(f"Studio: cannot remove {self.partPath}: {e}")

    def _onReadyRead(self):
        if self._reply is None or self._file is None:
            return
        data = self._reply.readAll()
        if data.size() > 0:
            self._file.write(data.data())

    def _onDownloadProgress(self, bytesReceived, bytesTotal):
        self.bytesReceived = int(bytesReceived)
        self.bytesTotal = int(bytesTotal) if bytesTotal > 0 else -1
        self.progress.emit(self.bytesReceived, self.bytesTotal)

    def _onFinished(self):
        reply = self._reply
        if reply is None or self.state != "running":
            return
        self._onReadyRead()
        error = reply.error()
        errorString = reply.errorString()
        status = reply.attribute(qt.QNetworkRequest.HttpStatusCodeAttribute)
        self._releaseReply()
        if self._file is not None:
            self._file.close()
            self._file = None
        if error != qt.QNetworkReply.NoError:
            self._closeAndRemovePartFile()
            self._fail(_("Download failed: {reason}").format(reason=errorString))
            return
        if status is not None and int(status) >= 400:
            self._closeAndRemovePartFile()
            self._fail(_("Download failed: the server answered {status}").format(status=int(status)))
            return
        if self.sha256 is not None:
            digest = sha256(self.partPath)
            if digest != self.sha256:
                self._closeAndRemovePartFile()
                logging.error(f"Studio: checksum mismatch for {self.url}: {digest} (expected {self.sha256})")
                self._fail(_("The downloaded file is damaged (checksum mismatch)"))
                return
        try:
            os.replace(self.partPath, self.dest)
        except OSError as e:
            self._closeAndRemovePartFile()
            self._fail(_("Cannot write {path}: {reason}").format(path=self.dest, reason=e.strerror or str(e)))
            return
        self.state = "done"
        self.finished.emit(self.dest)

    def _fail(self, message):
        self.state = "failed"
        self.errorMessage = message
        logging.warning(f"Studio: {message} ({self.url})")
        self.failed.emit(message)


#
# Banner
#

BANNER_WIDTH = 340
BANNER_MARGIN = 16


class DownloadBanner(qt.QObject):
    """Non-modal card at the bottom right of a widget (the view area) that offers a download.

    States: "offer" (Download / ✕), "running" (progress bar and Cancel), "done" (the action button, for
    example "Switch to 1 mm", and ✕) and "failed" (Retry / ✕). The card follows the size of its parent
    widget and never blocks the application.

    :param parentWidget: widget over which the card is shown (``mainWindow().centralWidget()``).
    :param offerText: text of the offer, for example "Download 1 mm template (13.7 MB) for finer tracing".
    :param url, sha256, dest: arguments of the ``Download``.
    :param readyText: text shown once the file is downloaded.
    :param actionText: text of the button shown once the file is downloaded.
    :param onAction: ``onAction(path)`` is called when that button is clicked; the card then closes.
    """

    def __init__(self, parentWidget, offerText, url, sha256, dest, readyText, actionText, onAction):
        qt.QObject.__init__(self)
        self.parentWidget = parentWidget
        self.offerText = offerText
        self.readyText = readyText
        self.onAction = onAction
        self.download = Download(url, sha256, dest)
        self.download.progress.connect(self._onProgress)
        self.download.finished.connect(self._onFinished)
        self.download.failed.connect(self._onFailed)
        self.state = None
        self._disposeOnQuit = False

        self.widget = qt.QFrame(parentWidget)
        self.widget.setObjectName("StudioDownloadBanner")
        self.widget.setFixedWidth(BANNER_WIDTH)
        # The card frame is drawn by the card role (a stylesheet bound to the card itself would replace it)
        ui.setRole(self.widget, "card")
        layout = qt.QVBoxLayout(self.widget)
        layout.setContentsMargins(12, 10, 8, 12)
        layout.setSpacing(8)

        headerRow = qt.QHBoxLayout()
        headerRow.setSpacing(8)
        self.iconLabel = qt.QLabel()
        self.iconLabel.setObjectName("StudioDownloadBannerIcon")
        self.iconLabel.setFixedSize(18, 18)
        headerRow.addWidget(self.iconLabel, 0, qt.Qt.AlignTop)
        self.textLabel = qt.QLabel()
        self.textLabel.setObjectName("StudioDownloadBannerText")
        self.textLabel.setWordWrap(True)
        headerRow.addWidget(self.textLabel, 1)
        self.closeButton = qt.QToolButton()
        self.closeButton.setObjectName("StudioDownloadBannerCloseButton")
        self.closeButton.setFixedSize(24, 24)
        self.closeButton.setIconSize(qt.QSize(14, 14))
        self.closeButton.setAutoRaise(True)
        self.closeButton.setToolTip(_("Close"))
        ui.setRole(self.closeButton, "ghost")
        self.closeButton.connect("clicked()", self.close)
        headerRow.addWidget(self.closeButton, 0, qt.Qt.AlignTop)
        layout.addLayout(headerRow)

        self.progressBar = qt.QProgressBar()
        self.progressBar.setObjectName("StudioDownloadBannerProgress")
        self.progressBar.setTextVisible(False)
        self.progressBar.setFixedHeight(6)
        self.progressBar.setRange(0, 0)
        layout.addWidget(self.progressBar)
        self.detailLabel = qt.QLabel()
        self.detailLabel.setObjectName("StudioDownloadBannerDetail")
        self._setPixelSize(self.detailLabel, 12)
        layout.addWidget(self.detailLabel)
        ui.bindStyleSheet(self.textLabel, lambda: f"QLabel#StudioDownloadBannerText {{ color: {ui.token('text').name()}; }}")
        ui.bindStyleSheet(self.detailLabel, lambda: f"QLabel#StudioDownloadBannerDetail {{ color: {ui.token('text2').name()}; }}")

        buttonRow = qt.QHBoxLayout()
        buttonRow.setContentsMargins(26, 0, 4, 0)
        buttonRow.setSpacing(8)
        buttonRow.addStretch(1)
        self.secondaryButton = qt.QPushButton()
        self.secondaryButton.setObjectName("StudioDownloadBannerSecondaryButton")
        ui.setRole(self.secondaryButton, "secondary")
        self.secondaryButton.connect("clicked()", self._onSecondaryClicked)
        buttonRow.addWidget(self.secondaryButton)
        self.primaryButton = qt.QPushButton()
        self.primaryButton.setObjectName("StudioDownloadBannerPrimaryButton")
        ui.setRole(self.primaryButton, "primary")
        self.primaryButton.connect("clicked()", self._onPrimaryClicked)
        buttonRow.addWidget(self.primaryButton)
        layout.addLayout(buttonRow)
        self.actionText = actionText

        ui.addPaletteListener(self._refreshIcons)
        self._refreshIcons()
        parentWidget.installEventFilter(self)
        # Python event filters must not run while the interpreter is finalized (after the application quits)
        slicer.app.connect("aboutToQuit()", self.dispose)
        self._disposeOnQuit = True
        self._setState("offer")

    @staticmethod
    def _setPixelSize(widget, pixelSize):
        font = qt.QFont(widget.font)
        font.setPixelSize(pixelSize)
        widget.setFont(font)

    def _refreshIcons(self):
        if not _isAlive(self.widget):
            return
        iconName = {"failed": "triangle-alert", "done": "check"}.get(self.state, "download")
        color = {"failed": "warning", "done": "success"}.get(self.state, "accent-fg")
        self.iconLabel.setPixmap(ui.pixmap(iconName, 18, color))
        self.closeButton.setIcon(ui.icon("x", 14))

    def isVisible(self):
        return _isAlive(self.widget) and self.widget.isVisible()

    def show(self):
        self.updatePosition()
        self.widget.show()
        self.widget.raise_()

    def close(self):
        """Close the card. A running download is canceled."""
        if self.download.isRunning():
            self.download.cancel()
        self.dispose()

    def dispose(self):
        if self._disposeOnQuit:
            slicer.app.disconnect("aboutToQuit()", self.dispose)
            self._disposeOnQuit = False
        ui.removePaletteListener(self._refreshIcons)
        self.download.progress.disconnect()
        self.download.finished.disconnect()
        self.download.failed.disconnect()
        if self.download.isRunning():
            self.download.cancel()
        if _isAlive(self.parentWidget):
            self.parentWidget.removeEventFilter(self)
        if _isAlive(self.widget):
            for widget in (self.widget, self.textLabel, self.detailLabel):
                ui.bindStyleSheet(widget, None)
            self.widget.hide()
            self.widget.deleteLater()

    def eventFilter(self, obj, event):
        if event.type() == qt.QEvent.Resize and obj == self.parentWidget:
            self.updatePosition()
        return False

    def updatePosition(self):
        if not _isAlive(self.widget):
            return
        self.widget.adjustSize()
        rect = self.parentWidget.rect
        self.widget.move(max(0, rect.width() - self.widget.width - BANNER_MARGIN),
                         max(0, rect.height() - self.widget.height - BANNER_MARGIN))

    def startDownload(self):
        self.download.start()
        if self.download.state == "running":
            self._setState("running")

    def _setState(self, state):
        self.state = state
        running = state == "running"
        self.progressBar.setVisible(running)
        self.detailLabel.setVisible(running or state == "failed")
        self.secondaryButton.setVisible(state in ("offer", "running", "failed"))
        self.primaryButton.setVisible(state in ("offer", "done", "failed"))
        if state == "offer":
            self.textLabel.setText(self.offerText)
            self.secondaryButton.setText(_("Not now"))
            self.primaryButton.setText(_("Download"))
        elif state == "running":
            self.textLabel.setText(self.offerText)
            self.secondaryButton.setText(_("Cancel"))
            self.progressBar.setRange(0, 0)
            self.detailLabel.setText(_("Starting download…"))
        elif state == "done":
            self.textLabel.setText(self.readyText)
            self.primaryButton.setText(self.actionText)
        elif state == "failed":
            self.textLabel.setText(self.offerText)
            self.detailLabel.setText(self.download.errorMessage)
            self.secondaryButton.setText(_("Not now"))
            self.primaryButton.setText(_("Retry"))
        self._refreshIcons()
        if self.widget.isVisible():
            self.updatePosition()

    def _onPrimaryClicked(self):
        if self.state in ("offer", "failed"):
            self.startDownload()
        elif self.state == "done":
            path = self.download.dest
            self.dispose()
            if self.onAction is not None:
                self.onAction(path)

    def _onSecondaryClicked(self):
        if self.state == "running":
            # Back to the offer: the download can be started again
            self.download.cancel()
        else:
            self.close()

    def _onProgress(self, bytesReceived, bytesTotal):
        if self.state != "running":
            return
        if bytesTotal > 0:
            self.progressBar.setRange(0, 1000)
            self.progressBar.setValue(int(1000 * bytesReceived / bytesTotal))
            self.detailLabel.setText(_("{received} of {total}").format(received=formatSize(bytesReceived), total=formatSize(bytesTotal)))
        else:
            self.progressBar.setRange(0, 0)
            self.detailLabel.setText(formatSize(bytesReceived))

    def _onFinished(self, path):
        self._setState("done")

    def _onFailed(self, message):
        if self.download.state == "canceled":
            self._setState("offer")
        else:
            self._setState("failed")

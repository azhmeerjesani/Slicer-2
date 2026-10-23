# Slicer Studio in a web browser.
#
# The Linux build of Slicer Studio runs on a virtual X display (TigerVNC Xvnc,
# software OpenGL with Mesa llvmpipe) and is shown in the browser with noVNC
# on port 8080. Nothing of the user interface is reimplemented: this is the
# desktop application, streamed.
#
# Build context: a folder with this Dockerfile, the docker/ folder next to it
# and a Slicer Linux package (Slicer-*-linux-amd64.tar.gz, "make package" of a
# Slicer build tree). The launcher assembles it:
#
#   python3 Utilities/SlicerStudioWeb/slicer_studio_web.py build --package Slicer-X.Y.Z-linux-amd64.tar.gz
#
# or by hand (BuildKit is required, the default since Docker 23):
#
#   docker build --file Dockerfile --build-arg SLICER_PACKAGE=Slicer-X.Y.Z-linux-amd64.tar.gz --tag slicer-studio-web CONTEXT_DIR
#
# Run (the launcher does this: python3 slicer_studio_web.py run):
#
#   docker run -d --name slicer-studio-web -p 127.0.0.1:8080:8080 -v "$HOME/SlicerStudioData:/data" --shm-size 2g slicer-studio-web
#
# The package is built against the Qt 5 of Ubuntu 24.04 (Slicer_USE_SYSTEM_QT),
# so the base image must stay Ubuntu 24.04; BASE_IMAGE only selects where it
# comes from (for example mirror.gcr.io/library/ubuntu:24.04).

ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Runtime libraries of the package (the system libraries it links to, found
# with readelf/ldd and mapped to packages), plugins that Qt loads at run time
# (SQLite driver of the DICOM database, SVG icons, WebEngine resources), the
# virtual display, the web server (websockify) and a window manager. Of the
# noVNC package only the static web files are needed: they are extracted from
# it, without its dependencies (Node.js and Python libraries for its tools).
RUN set -eux; \
  apt-get update; \
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates \
    fontconfig \
    fonts-dejavu-core \
    libcrypt1 \
    libgl1 \
    libgl1-mesa-dri \
    libglib2.0-0t64 \
    libglx-mesa0 \
    libice6 \
    libopengl0 \
    libpcre2-16-0 \
    libqt5core5t64 \
    libqt5gui5t64 \
    libqt5multimedia5 \
    libqt5multimediawidgets5 \
    libqt5network5t64 \
    libqt5opengl5t64 \
    libqt5positioning5 \
    libqt5printsupport5t64 \
    libqt5qml5 \
    libqt5qmlmodels5 \
    libqt5quick5 \
    libqt5quickwidgets5 \
    libqt5sql5-sqlite \
    libqt5sql5t64 \
    libqt5svg5 \
    libqt5webchannel5 \
    libqt5webengine5 \
    libqt5webenginecore5 \
    libqt5webenginewidgets5 \
    libqt5widgets5t64 \
    libqt5x11extras5 \
    libqt5xml5t64 \
    libqt5xmlpatterns5 \
    libsm6 \
    libssl3t64 \
    libuuid1 \
    libx11-6 \
    libx11-xcb1 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-render0 \
    libxcb-shape0 \
    libxcb-shm0 \
    libxcb-sync1 \
    libxcb-util1 \
    libxcb-xfixes0 \
    libxcb-xinerama0 \
    libxcb-xkb1 \
    libxcb1 \
    libxext6 \
    libxkbcommon-x11-0 \
    libxkbcommon0 \
    openbox \
    procps \
    tigervnc-standalone-server \
    tigervnc-tools \
    tini \
    websockify \
    zlib1g \
  ; \
  cd /tmp; \
  apt-get download novnc; \
  dpkg-deb --fsys-tarfile novnc_*.deb | tar -x -C / ./usr/share/novnc ./usr/share/doc/novnc; \
  rm novnc_*.deb; \
  test -f /usr/share/novnc/vnc.html; \
  rm -rf /var/lib/apt/lists/*

# User "slicer" (uid 1000, the uid of the "ubuntu" user it replaces). HOME is
# writable by any uid so the container can also run with --user (the launcher
# does this on Linux to write files with the owner of the data folder).
RUN set -eux; \
  if id ubuntu > /dev/null 2>&1; then userdel --remove ubuntu; fi; \
  useradd --create-home --uid 1000 --user-group --shell /bin/bash slicer; \
  chmod 1777 /home/slicer; \
  mkdir -p /data /tmp/.X11-unix; \
  chown slicer:slicer /data; \
  chmod 1777 /tmp/.X11-unix

# The build arguments are declared here, after the package installation, so
# that a new package or certificate does not invalidate the cached layers above.

# File name of the Slicer Linux package in the build context.
ARG SLICER_PACKAGE
# Optional CA certificate file (PEM) in the build context, for networks whose
# proxy inspects TLS: Slicer trusts it for its downloads (sample data,
# extensions). The Ubuntu packages come from the HTTP archive and need no
# certificate; the standard proxy variables (HTTP_PROXY, ...) are honored.
ARG EXTRA_CA_CERT_FILE=

# Slicer, extracted from the package without keeping the archive in a layer.
# Only the package is mounted, so that changes of other files of the build
# context do not invalidate this (large) layer.
RUN --mount=type=bind,source=${SLICER_PACKAGE},target=/mnt/slicer-package.tar.gz \
  set -eux; \
  test -n "${SLICER_PACKAGE}" || { echo "Set --build-arg SLICER_PACKAGE=<Slicer Linux package file name>"; exit 1; }; \
  mkdir -p /opt/slicer; \
  tar -xzf /mnt/slicer-package.tar.gz -C /opt/slicer --strip-components=1 --no-same-owner; \
  test -x /opt/slicer/Slicer

# Extra CA certificate: trusted by the system and by Slicer, which uses its own
# certificate bundle (share/Slicer-*/Slicer.crt).
RUN --mount=type=bind,target=/mnt/context \
  set -eux; \
  if [ -n "${EXTRA_CA_CERT_FILE}" ]; then \
    cp "/mnt/context/${EXTRA_CA_CERT_FILE}" /usr/local/share/ca-certificates/slicer-studio-web-extra-ca.crt; \
    update-ca-certificates; \
    for bundle in /opt/slicer/share/Slicer-*/Slicer.crt; do \
      if [ -f "${bundle}" ]; then cat /usr/local/share/ca-certificates/slicer-studio-web-extra-ca.crt >> "${bundle}"; fi; \
    done; \
  fi

COPY docker/entrypoint.sh /usr/local/bin/slicer-studio-web-entrypoint
COPY docker/openbox-rc.xml /etc/slicer-studio-web/openbox-rc.xml
COPY docker/index.html /usr/share/novnc/index.html
# A checkout with Windows line endings (Git for Windows converts to CRLF by
# default) would give a script that cannot run: convert it to LF.
# On Linux, Slicer keeps its revision-specific settings and extensions in
# <installation>/slicer.org, which the user cannot write here: keep them in the
# settings folder of the user instead (kept between runs with the home volume).
RUN set -eux; \
  sed -i 's/\r$//' /usr/local/bin/slicer-studio-web-entrypoint; \
  chmod 0755 /usr/local/bin/slicer-studio-web-entrypoint; \
  if [ ! -e /opt/slicer/slicer.org ]; then ln -s /home/slicer/.config/slicer.org /opt/slicer/slicer.org; fi

# Startup script of the web build (SLICERRC replaces ~/.slicerrc.py, so it runs that file too): the file
# dialogs of Slicer Studio start in /data (the data folder shared with the host) until the user picks
# another folder. It writes only the fork keys Studio/LastSaveFolder (save dialogs) and
# Studio/LastOpenDirectory (open dialogs), and only when no valid folder is set.
RUN set -eux; \
  mkdir -p /etc/slicer-studio-web; \
  printf '%s\n' \
    '# Slicer Studio web build: file dialogs start in /data.' \
    'import os' \
    'import qt' \
    'try:' \
    '    _settings = qt.QSettings()' \
    '    for _key in ("Studio/LastSaveFolder", "Studio/LastOpenDirectory"):' \
    '        _folder = str(_settings.value(_key) or "")' \
    '        if os.path.isdir("/data") and not (_folder and os.path.isdir(_folder)):' \
    '            _settings.setValue(_key, "/data")' \
    'except Exception as error:  # a settings problem must not stop the start-up' \
    '    print("Could not set the default folder of the file dialogs: %s" % error)' \
    '_userRc = os.path.expanduser("~/.slicerrc.py")' \
    'if os.path.isfile(_userRc):' \
    '    exec(compile(open(_userRc, encoding="utf-8").read(), _userRc, "exec"))' \
    > /etc/slicer-studio-web/slicerrc.py; \
  chmod 0644 /etc/slicer-studio-web/slicerrc.py

# SLICER_STUDIO_WEB=1 marks the browser build: Slicer Studio reads it to know that there is no image
# clipboard or host drag-and-drop, that the system browser cannot be seen (reports open in the app) and
# that connectome downloads must go to /data.
ENV LANG=C.UTF-8 \
    HOME=/home/slicer \
    SLICER_EXECUTABLE=/opt/slicer/Slicer \
    SLICER_STUDIO_WEB=1 \
    SLICERRC=/etc/slicer-studio-web/slicerrc.py

USER slicer
WORKDIR /home/slicer
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD python3 -c "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8080/', timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/slicer-studio-web-entrypoint"]

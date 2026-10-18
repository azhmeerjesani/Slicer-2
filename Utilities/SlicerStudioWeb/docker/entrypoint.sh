#!/bin/bash
# Entry point of the Slicer Studio web image.
#
# Starts a virtual X display with a VNC server (TigerVNC Xvnc, software OpenGL),
# a small window manager, the noVNC web client (served by websockify on port
# 8080) and Slicer. Slicer runs in a loop: if the user quits it, it is started
# again so the browser tab stays usable. Stopping the container stops everything.
#
# Environment:
#   GEOMETRY      initial screen size (default 1600x1000); the browser resizes it
#   VNC_PASSWORD  require this password in the browser (VNC authentication uses
#                 at most 8 characters); no password when empty
#   SLICER_ARGS   extra Slicer command-line arguments (word-split); arguments
#                 given to the container (docker run ... IMAGE ARGS) are added too

set -euo pipefail

readonly GEOMETRY="${GEOMETRY:-1600x1000}"
readonly DISPLAY_NUMBER=1
readonly VNC_PORT=5901
readonly WEB_PORT=8080
readonly NOVNC_DIR="${NOVNC_DIR:-/usr/share/novnc}"
readonly SLICER_EXECUTABLE="${SLICER_EXECUTABLE:-/opt/slicer/Slicer}"
readonly OPENBOX_CONFIG="/etc/slicer-studio-web/openbox-rc.xml"
# Process names (pgrep/pkill patterns) of the Slicer launcher and application.
readonly SLICER_PROCESS_NAMES="Slicer|SlicerApp-real"

export DISPLAY=":${DISPLAY_NUMBER}"

log() {
  echo "[slicer-studio-web] $*"
}

# The container may run with an arbitrary --user id (the launcher does this on
# Linux to match the owner of the data folder): make sure HOME is writable.
user_id="$(id -u)"
if [ ! -w "${HOME:-/nonexistent}" ]; then
  HOME="/tmp/home-${user_id}"
  export HOME
fi
mkdir -p "${HOME}"
# Such an id has no name in /etc/passwd ("id -un" prints the number and fails):
# use the name of the image user, which Slicer uses for its temporary folder.
if [ -z "${USER:-}" ] && ! USER="$(id -un 2> /dev/null)"; then
  USER=slicer
fi
LOGNAME="${USER}"
XDG_RUNTIME_DIR="/tmp/runtime-${user_id}"
export USER LOGNAME XDG_RUNTIME_DIR
mkdir -p "${XDG_RUNTIME_DIR}"
chmod 700 "${XDG_RUNTIME_DIR}"

# Folder of the revision-specific settings and extensions of Slicer
# (/opt/slicer/slicer.org links to it, see the Dockerfile).
mkdir -p /home/slicer/.config/slicer.org 2> /dev/null || true

# Slicer's default scene folder is ~/Documents: point it to the data volume.
if [ ! -e "${HOME}/Documents" ] && [ -d /data ]; then
  ln -s /data "${HOME}/Documents"
fi

vnc_security=(-SecurityTypes None)
if [ -n "${VNC_PASSWORD:-}" ]; then
  if [ "${#VNC_PASSWORD}" -gt 8 ]; then
    log "Note: VNC authentication only uses the first 8 characters of VNC_PASSWORD."
  fi
  password_file="${XDG_RUNTIME_DIR}/vncpasswd"
  printf '%s\n' "${VNC_PASSWORD}" | vncpasswd -f > "${password_file}"
  chmod 600 "${password_file}"
  vnc_security=(-SecurityTypes VncAuth -PasswordFile "${password_file}")
fi
# Do not pass the password on to Slicer and its subprocesses.
unset VNC_PASSWORD

slicer_pid=""
pids=()

stop_all() {
  trap - TERM INT HUP
  log "Stopping."
  if [ -n "${slicer_pid}" ]; then
    kill -TERM "${slicer_pid}" 2> /dev/null || true
  fi
  # Also stop instances that Slicer started itself (for example after "Restart").
  pkill -TERM -u "${user_id}" -x "${SLICER_PROCESS_NAMES}" 2> /dev/null || true
  for pid in "${pids[@]}"; do
    kill -TERM "${pid}" 2> /dev/null || true
  done
  wait || true
  exit "${1:-0}"
}
trap 'stop_all 0' TERM INT HUP

# Remove stale files of a previous run of this container (docker restart).
rm -f "/tmp/.X${DISPLAY_NUMBER}-lock" "/tmp/.X11-unix/X${DISPLAY_NUMBER}"

log "Starting the virtual display (${GEOMETRY})."
Xvnc "${DISPLAY}" \
  -geometry "${GEOMETRY}" \
  -depth 24 \
  -desktop "Slicer Studio" \
  -rfbport "${VNC_PORT}" \
  -localhost \
  -nolisten tcp \
  -AlwaysShared \
  -AcceptSetDesktopSize \
  "${vnc_security[@]}" &
xvnc_pid=$!
pids+=("${xvnc_pid}")

for _ in $(seq 1 300); do
  [ -S "/tmp/.X11-unix/X${DISPLAY_NUMBER}" ] && break
  if ! kill -0 "${xvnc_pid}" 2> /dev/null; then
    log "Error: the virtual display (Xvnc) failed to start."
    stop_all 1
  fi
  sleep 0.1
done

openbox --config-file "${OPENBOX_CONFIG}" &
pids+=("$!")

log "Starting the web client on port ${WEB_PORT}."
websockify --web "${NOVNC_DIR}" --heartbeat 30 "0.0.0.0:${WEB_PORT}" "127.0.0.1:${VNC_PORT}" &
websockify_pid=$!
pids+=("${websockify_pid}")

# Software rendering (no GPU in the container) and Qt WebEngine settings that
# work in an unprivileged container.
export LIBGL_ALWAYS_SOFTWARE=1
export QTWEBENGINE_DISABLE_SANDBOX=1
export QTWEBENGINE_CHROMIUM_FLAGS="${QTWEBENGINE_CHROMIUM_FLAGS:---disable-gpu --disable-dev-shm-usage}"

read -r -a slicer_args <<< "${SLICER_ARGS:-}"
slicer_args+=("$@")

# Start in HOME, where file dialogs show Documents (the data volume). Not in
# /data itself: when the working directory is the Documents folder, the
# directory button of the settings panel resolves the default scene path to
# the Slicer installation folder.
cd "${HOME}"

quick_exits=0
while true; do
  started=${SECONDS}
  log "Starting Slicer."
  "${SLICER_EXECUTABLE}" "${slicer_args[@]}" &
  slicer_pid=$!
  status=0
  wait -n "${slicer_pid}" "${xvnc_pid}" "${websockify_pid}" || status=$?
  if ! kill -0 "${xvnc_pid}" 2> /dev/null || ! kill -0 "${websockify_pid}" 2> /dev/null; then
    log "Error: the virtual display or the web server stopped."
    stop_all 1
  fi
  slicer_pid=""
  # "Restart Slicer" starts a new instance (launcher, then application) and quits
  # the current one: wait for that instance instead of starting another one.
  if pgrep -u "${user_id}" -x "${SLICER_PROCESS_NAMES}" > /dev/null; then
    log "Slicer restarted itself; waiting for the new instance to exit."
    while pgrep -u "${user_id}" -x "${SLICER_PROCESS_NAMES}" > /dev/null; do
      sleep 1 &
      wait $! || true
    done
  fi
  if ((SECONDS - started < 20)); then
    quick_exits=$((quick_exits + 1))
  else
    quick_exits=0
  fi
  delay=$((quick_exits < 5 ? 1 + quick_exits * quick_exits : 30))
  log "Slicer exited (status ${status}); starting it again in ${delay} s."
  sleep "${delay}" &
  wait $! || true
done

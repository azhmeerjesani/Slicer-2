#!/usr/bin/env bash
# Add Slicer Studio to the applications menu of the current user (Linux).
#
# The Linux package is a folder that can be extracted anywhere, so the menu
# entry needs the absolute path of its launcher. Run this script from the
# extracted folder (where the "Slicer" launcher is):
#
#   ./install-desktop-entry.sh              add or update the menu entry and icons
#   ./install-desktop-entry.sh --uninstall  remove them
#
# It writes only into ~/.local/share (XDG_DATA_HOME): applications/slicer-studio.desktop
# and icons/hicolor/<size>/apps/slicer-studio.png. Nothing is written into the package.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
data_home=${XDG_DATA_HOME:-"${HOME}/.local/share"}
entry="${data_home}/applications/slicer-studio.desktop"
sizes=(48 128 256 512)

refresh() {
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "${data_home}/applications" >/dev/null 2>&1 || true
  fi
  if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t "${data_home}/icons/hicolor" >/dev/null 2>&1 || true
  fi
}

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "${entry}"
  for size in "${sizes[@]}"; do
    rm -f "${data_home}/icons/hicolor/${size}x${size}/apps/slicer-studio.png"
  done
  refresh
  echo "Removed the Slicer Studio menu entry."
  exit 0
fi
if [ $# -gt 0 ]; then
  echo "usage: $0 [--uninstall]" >&2
  exit 2
fi

launcher="${here}/Slicer"
if [ ! -x "${launcher}" ]; then
  echo "error: no Slicer launcher next to this script (${launcher})" >&2
  exit 1
fi
# The icons and the entry template are installed in share/Slicer-<version>/desktop
desktop_dir=""
for candidate in "${here}"/share/Slicer-*/desktop; do
  if [ -f "${candidate}/slicer-studio.desktop.in" ]; then
    desktop_dir=${candidate}
  fi
done
if [ -z "${desktop_dir}" ]; then
  echo "error: share/Slicer-<version>/desktop not found in ${here}" >&2
  exit 1
fi

mkdir -p "${data_home}/applications"
for size in "${sizes[@]}"; do
  mkdir -p "${data_home}/icons/hicolor/${size}x${size}/apps"
  cp "${desktop_dir}/icons/hicolor/${size}x${size}/apps/slicer-studio.png" \
    "${data_home}/icons/hicolor/${size}x${size}/apps/slicer-studio.png"
done
# Desktop entry quoting: backslash, double quote, backtick and dollar are escaped inside the quoted Exec path
escaped=$(printf '%s' "${launcher}" | sed -e 's/[\\"`$]/\\\\&/g')
escaped_for_sed=$(printf '%s' "${escaped}" | sed -e 's/[&|\\]/\\&/g')
sed -e "s|@LAUNCHER@|${escaped_for_sed}|" "${desktop_dir}/slicer-studio.desktop.in" >"${entry}.tmp"
mv "${entry}.tmp" "${entry}"
chmod 644 "${entry}"
refresh
echo "Added Slicer Studio to the applications menu (${entry})."

#!/usr/bin/env bash
#
# Build an installable "Slicer Studio" disk image (.dmg) on macOS.
#
# One command, run from a checkout of this repository on a Mac:
#
#   Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh
#
# It checks the machine, installs the build tools with Homebrew, gets Qt,
# configures and builds the Slicer superbuild (Release), packages it with CPack
# and turns the package into "SlicerStudio-<version>-macos-<arch>.dmg". The
# application inside is "Slicer Studio.app" with its own bundle identifier, so
# it installs next to an official "3D Slicer" application without replacing it.
#
# Re-running the script continues an interrupted build. Run it with --help for
# the options and see README.md next to this script for details.
#
# The script is written for the bash 3.2 that ships with macOS.

set -u
set -o pipefail

SCRIPT_NAME=$(basename "$0")
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

#-----------------------------------------------------------------------------
# Constants
#-----------------------------------------------------------------------------

# Exit codes (also listed in --help and README.md)
EXIT_FAILURE=1
EXIT_USAGE=2
EXIT_PREFLIGHT=3
EXIT_DEPS=4
EXIT_CONFIGURE=5
EXIT_BUILD=6
EXIT_PACKAGE=7
EXIT_SMOKE=8

APP_NAME="Slicer Studio"
APP_BUNDLE_ID="org.slicer.slicerstudio"
DMG_PREFIX="SlicerStudio"

# Qt version used by --qt=official and --qt=source: the version
# Utilities/Scripts/Build-qt6.sh builds by default.
DEFAULT_QT_VERSION="6.11.2"
AQTINSTALL_VERSION="3.3.0"
# Qt add-on modules Slicer needs on top of the essentials (qtbase, qtdeclarative,
# qtsvg, qttools, qttranslations) that aqtinstall always installs.
OFFICIAL_QT_MODULES="qt5compat qtimageformats qtmultimedia qtpositioning qtscxml qtshadertools qtwebchannel qtwebengine"

# Oldest macOS a build can target. The source tree enforces its own minimum
# (required_deployment_target in CMake/SlicerInitializeOSXVariables.cmake),
# which is read from there; this value is used when it cannot be read.
DEFAULT_MACOS_MIN="14.0"
MIN_FREE_DISK_GB=40
MIN_CMAKE_VERSION="3.28"
# CMake used when no CMake 3 (3.28 or newer) is installed: official universal
# binaries from Kitware, checked against their published SHA-256. This source
# tree validates CMake policies up to 3.28 and the macOS build instructions use
# 3.31, while Homebrew (and GitHub's runners) provide CMake 4.
PINNED_CMAKE_VERSION="3.31.10"
PINNED_CMAKE_SHA256="be9f3faeeaf7921cc2d77cea711dd5e6f72c63af2810cacd9205b3ce8d1593c9"
FINGERPRINT_NAME=".slicer-studio-build.conf"

#-----------------------------------------------------------------------------
# Options
#-----------------------------------------------------------------------------

QT_SPEC="homebrew"
SOURCE_DIR=""
WORK_DIR=""
BUILD_DIR=""
OUT_DIR=""
JOBS=""
MACOS_MIN=""
CMAKE_EXTRA_ARGS=()
DO_CLEAN=0
RECONFIGURE=0
SKIP_DEPS=0
SKIP_BUILD=0
NO_PACKAGE=0
NO_SIGN=0
SIGN_IDENTITY="-"
NO_SMOKE=0
SMOKE_ONLY=0
SMOKE_DMG=""
DMG_FORMAT="UDZO"
CI_MODE=0
ASSUME_YES=0
DRY_RUN=0

#-----------------------------------------------------------------------------
# State
#-----------------------------------------------------------------------------

HOST_OS=""
HOST_ARCH=""
HOST_MACOS_VERSION=""
FAKED_PLATFORM=0
MACOS_FLOOR=${DEFAULT_MACOS_MIN}
BREW=""
BREW_PREFIX=""
CMAKE_BIN="cmake"
QT_MODE=""
QT_REQUESTED_VERSION=""
QT_CMAKE_VAR=""
QT_CMAKE_DIR=""
QT_VERSION_FOUND=""
QT_MAJOR_MINOR=""
QT_MINOS=""
SLICER_BUILD_DIR=""
LOG_DIR=""
CPACK_DMG=""
FINAL_DMG=""
PACKAGE_VERIFIED="not run"
C_COMPILER=""
CXX_COMPILER=""
MOUNTED_DIRS=""
ATTACHED_MOUNT=""
BG_PID=""
IN_GROUP=0
USE_COLOR=0
PREFLIGHT_PROBLEMS=""

#-----------------------------------------------------------------------------
# Output helpers
#-----------------------------------------------------------------------------

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} [options]

Build "${APP_NAME}.app" from this checkout and package it as
${DMG_PREFIX}-<version>-macos-<arch>.dmg (run on macOS ${DEFAULT_MACOS_MIN} or newer).

Qt:
  --qt=homebrew          Homebrew's Qt 6 (default). Fastest on Apple Silicon
                         with macOS 15 or newer; the DMG runs on this Mac and
                         on Macs with the same or a newer macOS.
  --qt=official[=VER]    Official Qt binaries (default ${DEFAULT_QT_VERSION}) downloaded with
                         aqtinstall. Redistributable DMG for macOS ${DEFAULT_MACOS_MIN} and newer.
  --qt=source[=VER]      Build Qt (default ${DEFAULT_QT_VERSION}) from source with
                         Utilities/Scripts/Build-qt6.sh. Redistributable, but
                         takes hours and needs the full Xcode.
  --qt=/path/to/Qt       An existing Qt 6 (>= 6.8) or Qt 5.15 installation: its
                         prefix, or the lib/cmake/Qt6 (or Qt5) directory.

Locations:
  --source DIR           Slicer source tree (default: the checkout containing
                         this script).
  --work-dir DIR         Downloads, tools, Qt, logs and staging
                         (default: ~/SlicerStudio).
  --build-dir DIR        Superbuild directory; keep the path short and without
                         spaces (default: <work-dir>/S).
  --out-dir DIR          Where the final DMG is written (default: <work-dir>/dist).

Build:
  --jobs N               Parallel build jobs (default: CPU count, capped at one
                         job per 2 GB of memory).
  --macos-min VERSION    Minimum macOS version of the build (default and lowest
                         allowed: the source tree's minimum, ${DEFAULT_MACOS_MIN}; raised
                         automatically to the minimum of the Qt libraries).
  --cmake-arg ARG        Extra argument for the superbuild configuration, for
                         example --cmake-arg=-DSlicer_USE_SimpleITK:BOOL=OFF
                         (repeatable).
  --clean                Delete the build directory and start from scratch.
  --reconfigure          Run the CMake configuration again on an existing build.
  --skip-deps            Do not install anything with Homebrew or download Qt
                         or CMake.
  --skip-build           Package only (the build must be complete).
  --no-package           Build only; stop before packaging.

Package:
  --no-sign              Do not code-sign the renamed application (it is then
                         left with a broken signature: sign it yourself).
  --sign-identity ID     Signing identity (default "-": ad-hoc signature).
  --dmg-format FORMAT    hdiutil image format of the final DMG (default UDZO).
  --no-smoke             Skip the start-up test of the packaged application.
  --smoke-only           Only run the start-up test on the newest DMG in the
                         output directory (or the one given with --dmg PATH).

General:
  --ci                   Non-interactive and quieter output (build output goes
                         to the log file, with a progress line every 5 minutes).
  --yes                  Answer yes to questions (deleting a build, installing
                         Rosetta 2).
  --dry-run              Print the plan and the commands without running them.
                         Works on any system; on Linux, set
                         SLICER_STUDIO_FAKE_UNAME=Darwin/arm64[/<macOS version>].
  -h, --help             Show this help.

Exit codes:
  0 success, 1 unexpected failure, 2 usage error, 3 preflight check failed,
  4 dependency installation failed, 5 configuration failed, 6 build failed,
  7 packaging failed, 8 start-up test failed.
EOF
}

setup_colors() {
  if [ "${CI_MODE}" = 0 ] && [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    USE_COLOR=1
  fi
}

color() {
  # color <ansi code> <text>
  if [ "${USE_COLOR}" = 1 ]; then
    printf '\033[%sm%s\033[0m' "$1" "$2"
  else
    printf '%s' "$2"
  fi
}

end_group() {
  if [ "${IN_GROUP}" = 1 ]; then
    echo "::endgroup::"
    IN_GROUP=0
  fi
}

section() {
  end_group
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    echo "::group::${APP_NAME}: $*"
    IN_GROUP=1
  else
    echo ""
    printf '%s\n' "$(color '1;34' "==> $*")"
  fi
}

info() {
  echo "    $*"
}

warn() {
  echo "$(color '1;33' 'warning:') $*" >&2
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    echo "::warning::$*"
  fi
}

die() {
  # die <exit code> <message>
  local code=$1
  shift
  end_group
  echo "$(color '1;31' 'error:') $*" >&2
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    echo "::error::$*"
  fi
  exit "${code}"
}

quote() {
  # Quote an argument for display, so printed commands can be copied.
  case "$1" in
    "" | *[!A-Za-z0-9_./:=@%+,-]*)
      printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
      ;;
    *) printf '%s' "$1" ;;
  esac
}

print_cmd() {
  local line="    +" argument
  if [ "${DRY_RUN}" = 1 ]; then
    line="    [dry-run] +"
  fi
  for argument in "$@"; do
    line="${line} $(quote "${argument}")"
  done
  printf '%s\n' "${line}"
}

run() {
  # Run a command (or only print it with --dry-run).
  print_cmd "$@"
  if [ "${DRY_RUN}" = 1 ]; then
    return 0
  fi
  "$@"
}

format_duration() {
  local seconds=$1
  printf '%dh%02dm' $((seconds / 3600)) $(((seconds % 3600) / 60))
}

run_logged() {
  # run_logged <log file> <command...>
  # Runs a long command. With --ci, its output only goes to the log file and a
  # progress line is printed every 5 minutes; otherwise it is also shown.
  local log_file=$1
  shift
  print_cmd "$@"
  info "log: ${log_file}"
  if [ "${DRY_RUN}" = 1 ]; then
    return 0
  fi
  mkdir -p "$(dirname "${log_file}")"
  local status=0
  if [ "${CI_MODE}" = 1 ]; then
    local start=${SECONDS}
    local ticks=0
    "$@" >>"${log_file}" 2>&1 &
    BG_PID=$!
    while kill -0 "${BG_PID}" 2>/dev/null; do
      sleep 10
      ticks=$((ticks + 1))
      if [ $((ticks % 30)) = 0 ]; then
        echo "    ... $(format_duration $((SECONDS - start))) elapsed: $(tail -n 1 "${log_file}" 2>/dev/null | cut -c1-150)"
      fi
    done
    wait "${BG_PID}" || status=$?
    BG_PID=""
    if [ "${status}" != 0 ]; then
      end_group
      echo "    last 150 lines of ${log_file}:"
      tail -n 150 "${log_file}" | sed 's/^/    | /'
    fi
  else
    "$@" 2>&1 | tee -a "${log_file}"
    status=${PIPESTATUS[0]}
  fi
  return "${status}"
}

run_with_timeout() {
  # run_with_timeout <seconds> <command...> (macOS has no timeout(1))
  local seconds=$1
  shift
  "$@" &
  local pid=$!
  (
    waited=0
    while kill -0 "${pid}" 2>/dev/null; do
      if [ "${waited}" -ge "${seconds}" ]; then
        echo "timeout: stopping after ${seconds} s: $*" >&2
        kill -TERM "${pid}" 2>/dev/null
        waited=0
        while kill -0 "${pid}" 2>/dev/null && [ "${waited}" -lt 15 ]; do
          sleep 1
          waited=$((waited + 1))
        done
        kill -KILL "${pid}" 2>/dev/null
        break
      fi
      sleep 1
      waited=$((waited + 1))
    done
  ) &
  local watcher=$!
  local status=0
  wait "${pid}" || status=$?
  wait "${watcher}" 2>/dev/null
  return "${status}"
}

confirm() {
  # confirm <question>: returns 0 for yes
  if [ "${ASSUME_YES}" = 1 ] || [ "${CI_MODE}" = 1 ]; then
    return 0
  fi
  if [ ! -t 0 ]; then
    return 1
  fi
  local answer=""
  printf '%s [y/N] ' "$1"
  read -r answer
  case "${answer}" in
    y | Y | yes | YES) return 0 ;;
    *) return 1 ;;
  esac
}

cleanup() {
  local status=$?
  if [ -n "${BG_PID}" ]; then
    kill "${BG_PID}" 2>/dev/null
  fi
  local mount_point
  for mount_point in ${MOUNTED_DIRS}; do
    hdiutil detach "${mount_point}" -quiet >/dev/null 2>&1 || hdiutil detach "${mount_point}" -force -quiet >/dev/null 2>&1
    rmdir "${mount_point}" 2>/dev/null
  done
  end_group
  exit "${status}"
}

#-----------------------------------------------------------------------------
# Small utilities
#-----------------------------------------------------------------------------

abspath() {
  local path=$1
  case "${path}" in
    \~) path="${HOME}" ;;
    \~/*) path="${HOME}/${path#\~/}" ;;
  esac
  case "${path}" in
    /*) ;;
    *) path="${PWD}/${path}" ;;
  esac
  # Drop trailing slashes (but keep "/")
  while [ "${#path}" -gt 1 ] && [ "${path%/}" != "${path}" ]; do
    path=${path%/}
  done
  printf '%s' "${path}"
}

version_ge() {
  # version_ge A B: true if version A >= version B (numeric, up to 3 fields)
  local a=() b=()
  IFS=. read -r -a a <<<"$1"
  IFS=. read -r -a b <<<"$2"
  local index x y
  for index in 0 1 2; do
    x=${a[index]:-0}
    y=${b[index]:-0}
    x=${x%%[!0-9]*}
    y=${y%%[!0-9]*}
    x=${x:-0}
    y=${y:-0}
    if [ "${x}" -gt "${y}" ]; then
      return 0
    fi
    if [ "${x}" -lt "${y}" ]; then
      return 1
    fi
  done
  return 0
}

major_minor() {
  local fields=()
  IFS=. read -r -a fields <<<"$1"
  printf '%s.%s' "${fields[0]:-0}" "${fields[1]:-0}"
}

existing_ancestor() {
  local path=$1
  while [ ! -d "${path}" ] && [ "${path}" != "/" ]; do
    path=$(dirname "${path}")
  done
  printf '%s' "${path}"
}

free_disk_gb() {
  df -Pk "$(existing_ancestor "$1")" 2>/dev/null | awk 'NR == 2 { print int($4 / 1048576) }'
}

cpu_count() {
  sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4
}

memory_gb() {
  local bytes
  bytes=$(sysctl -n hw.memsize 2>/dev/null || echo 0)
  echo $((bytes / 1073741824))
}

add_preflight_problem() {
  PREFLIGHT_PROBLEMS="${PREFLIGHT_PROBLEMS}  - $*
"
}

#-----------------------------------------------------------------------------
# Argument parsing
#-----------------------------------------------------------------------------

parse_args() {
  local option value inline_value
  while [ $# -gt 0 ]; do
    option=$1
    value=""
    inline_value=0
    case "${option}" in
      --*=*)
        value=${option#*=}
        option=${option%%=*}
        inline_value=1
        ;;
    esac
    case "${option}" in
      --qt | --source | --work-dir | --build-dir | --out-dir | --jobs | --macos-min | --cmake-arg | --sign-identity | --dmg-format | --dmg)
        if [ "${inline_value}" = 0 ]; then
          if [ $# -lt 2 ]; then
            die "${EXIT_USAGE}" "${option} requires a value (see --help)"
          fi
          value=$2
          shift
        fi
        if [ -z "${value}" ]; then
          die "${EXIT_USAGE}" "${option} requires a value (see --help)"
        fi
        ;;
    esac
    case "${option}" in
      --qt) QT_SPEC=${value} ;;
      --source) SOURCE_DIR=$(abspath "${value}") ;;
      --work-dir) WORK_DIR=$(abspath "${value}") ;;
      --build-dir) BUILD_DIR=$(abspath "${value}") ;;
      --out-dir) OUT_DIR=$(abspath "${value}") ;;
      --jobs) JOBS=${value} ;;
      --macos-min) MACOS_MIN=${value} ;;
      --cmake-arg) CMAKE_EXTRA_ARGS+=("${value}") ;;
      --sign-identity) SIGN_IDENTITY=${value} ;;
      --dmg-format) DMG_FORMAT=${value} ;;
      --dmg) SMOKE_DMG=$(abspath "${value}") ;;
      --clean) DO_CLEAN=1 ;;
      --reconfigure) RECONFIGURE=1 ;;
      --skip-deps) SKIP_DEPS=1 ;;
      --skip-build) SKIP_BUILD=1 ;;
      --no-package) NO_PACKAGE=1 ;;
      --no-sign) NO_SIGN=1 ;;
      --no-smoke) NO_SMOKE=1 ;;
      --smoke-only) SMOKE_ONLY=1 ;;
      --ci) CI_MODE=1 ;;
      --yes | -y) ASSUME_YES=1 ;;
      --dry-run) DRY_RUN=1 ;;
      -h | --help)
        usage
        exit 0
        ;;
      *) die "${EXIT_USAGE}" "unknown option: $1 (see --help)" ;;
    esac
    shift
  done

  if [ -n "${JOBS}" ]; then
    case "${JOBS}" in
      *[!0-9]* | ??????*) die "${EXIT_USAGE}" "--jobs expects a positive number, got '${JOBS}'" ;;
    esac
    # Base 10, so that a leading zero is not read as an octal number.
    JOBS=$((10#${JOBS}))
    if [ "${JOBS}" -lt 1 ]; then
      die "${EXIT_USAGE}" "--jobs expects a positive number, got '${JOBS}'"
    fi
  fi
  if [ -n "${MACOS_MIN}" ]; then
    case "${MACOS_MIN}" in
      *[!0-9.]* | .* | *. | *..*) die "${EXIT_USAGE}" "--macos-min expects a version such as ${DEFAULT_MACOS_MIN}, got '${MACOS_MIN}'" ;;
    esac
  fi
  if [ "${SKIP_BUILD}" = 1 ] && [ "${NO_PACKAGE}" = 1 ]; then
    die "${EXIT_USAGE}" "--skip-build and --no-package together leave nothing to do"
  fi
  if [ "${SMOKE_ONLY}" = 1 ] && [ "${NO_SMOKE}" = 1 ]; then
    die "${EXIT_USAGE}" "--smoke-only and --no-smoke cannot be combined"
  fi
  if [ "${DO_CLEAN}" = 1 ] && [ "${SKIP_BUILD}" = 1 ]; then
    die "${EXIT_USAGE}" "--clean and --skip-build cannot be combined"
  fi

  case "${QT_SPEC}" in
    homebrew)
      QT_MODE=homebrew
      ;;
    official | official=*)
      QT_MODE=official
      QT_REQUESTED_VERSION=${QT_SPEC#official}
      QT_REQUESTED_VERSION=${QT_REQUESTED_VERSION#=}
      QT_REQUESTED_VERSION=${QT_REQUESTED_VERSION:-${DEFAULT_QT_VERSION}}
      ;;
    source | source=*)
      QT_MODE=source
      QT_REQUESTED_VERSION=${QT_SPEC#source}
      QT_REQUESTED_VERSION=${QT_REQUESTED_VERSION#=}
      QT_REQUESTED_VERSION=${QT_REQUESTED_VERSION:-${DEFAULT_QT_VERSION}}
      ;;
    /* | ./* | ../* | \~/*)
      QT_MODE=path
      QT_SPEC=$(abspath "${QT_SPEC}")
      ;;
    *)
      die "${EXIT_USAGE}" "--qt expects homebrew, official[=VERSION], source[=VERSION] or a path, got '${QT_SPEC}'"
      ;;
  esac
  if [ -n "${QT_REQUESTED_VERSION}" ]; then
    case "${QT_REQUESTED_VERSION}" in
      6.[0-9]*.[0-9]*) ;;
      *) die "${EXIT_USAGE}" "Qt version must look like 6.Y.Z, got '${QT_REQUESTED_VERSION}'" ;;
    esac
    if ! version_ge "${QT_REQUESTED_VERSION}" "6.8"; then
      die "${EXIT_USAGE}" "Slicer needs Qt 6.8 or newer, got '${QT_REQUESTED_VERSION}'"
    fi
  fi
}

#-----------------------------------------------------------------------------
# Platform and preflight checks
#-----------------------------------------------------------------------------

detect_platform() {
  if [ -n "${SLICER_STUDIO_FAKE_UNAME:-}" ]; then
    if [ "${DRY_RUN}" != 1 ]; then
      die "${EXIT_USAGE}" "SLICER_STUDIO_FAKE_UNAME is only honored together with --dry-run"
    fi
    IFS=/ read -r HOST_OS HOST_ARCH HOST_MACOS_VERSION <<<"${SLICER_STUDIO_FAKE_UNAME}"
    HOST_MACOS_VERSION=${HOST_MACOS_VERSION:-15.0}
    FAKED_PLATFORM=1
  else
    HOST_OS=$(uname -s)
    HOST_ARCH=$(uname -m)
    if [ "${HOST_OS}" = "Darwin" ]; then
      HOST_MACOS_VERSION=$(sw_vers -productVersion 2>/dev/null || echo "")
    fi
  fi

  if [ "${HOST_OS}" != "Darwin" ]; then
    if [ "${DRY_RUN}" = 1 ]; then
      die "${EXIT_PREFLIGHT}" "this builds a macOS application and must run on macOS (to preview the plan elsewhere: SLICER_STUDIO_FAKE_UNAME=Darwin/arm64 ${SCRIPT_NAME} --dry-run)"
    fi
    die "${EXIT_PREFLIGHT}" "this builds a macOS application and must run on macOS (found ${HOST_OS}). On Linux, use the CPack tarball or Utilities/SlicerStudioWeb instead."
  fi
  case "${HOST_ARCH}" in
    arm64 | x86_64) ;;
    *) die "${EXIT_PREFLIGHT}" "unsupported CPU architecture '${HOST_ARCH}' (expected arm64 or x86_64)" ;;
  esac
}

find_brew() {
  if [ "${FAKED_PLATFORM}" = 1 ]; then
    BREW="brew"
    if [ "${HOST_ARCH}" = "arm64" ]; then
      BREW_PREFIX="/opt/homebrew"
    else
      BREW_PREFIX="/usr/local"
    fi
    return 0
  fi
  # Prefer the Homebrew of the build architecture: an Apple Silicon Mac can
  # also have an Intel Homebrew in /usr/local.
  local candidates=(/usr/local/bin/brew /opt/homebrew/bin/brew)
  if [ "${HOST_ARCH}" = "arm64" ]; then
    candidates=(/opt/homebrew/bin/brew /usr/local/bin/brew)
  fi
  local candidate
  for candidate in "${candidates[@]}" "$(command -v brew 2>/dev/null)"; do
    if [ -n "${candidate}" ] && [ -x "${candidate}" ]; then
      BREW=${candidate}
      BREW_PREFIX=$("${BREW}" --prefix 2>/dev/null)
      # Make Homebrew's tools (ccache, git-lfs) visible to the build.
      case ":${PATH}:" in
        *":${BREW_PREFIX}/bin:"*) ;;
        *) PATH="${BREW_PREFIX}/bin:${PATH}" ;;
      esac
      export PATH
      return 0
    fi
  done
  return 1
}

resolve_locations() {
  if [ -z "${SOURCE_DIR}" ]; then
    SOURCE_DIR=$(cd "${SCRIPT_DIR}/../../.." && pwd)
  fi
  WORK_DIR=${WORK_DIR:-${HOME}/SlicerStudio}
  BUILD_DIR=${BUILD_DIR:-${WORK_DIR}/S}
  OUT_DIR=${OUT_DIR:-${WORK_DIR}/dist}
  SLICER_BUILD_DIR="${BUILD_DIR}/Slicer-build"
  LOG_DIR="${WORK_DIR}/logs"
}

source_minimum_macos() {
  # Lowest CMAKE_OSX_DEPLOYMENT_TARGET accepted by the source tree.
  sed -n 's/^ *set(required_deployment_target "\([0-9.]*\)").*/\1/p' \
    "${SOURCE_DIR}/CMake/SlicerInitializeOSXVariables.cmake" 2>/dev/null | head -n 1
}

resolve_minimum_macos() {
  local minimum
  minimum=$(source_minimum_macos)
  MACOS_FLOOR=${minimum:-${DEFAULT_MACOS_MIN}}
  if [ -n "${MACOS_MIN}" ] && ! version_ge "${MACOS_MIN}" "${MACOS_FLOOR}"; then
    die "${EXIT_USAGE}" "--macos-min ${MACOS_MIN} is too old: this source tree builds for macOS ${MACOS_FLOOR} and newer (CMake/SlicerInitializeOSXVariables.cmake)"
  fi
}

preflight() {
  section "Preflight checks"
  info "source:      ${SOURCE_DIR}"
  info "work dir:    ${WORK_DIR}"
  info "build dir:   ${BUILD_DIR}"
  info "output dir:  ${OUT_DIR}"
  if [ "${FAKED_PLATFORM}" = 1 ]; then
    info "platform:    macOS ${HOST_MACOS_VERSION} ${HOST_ARCH} (faked with SLICER_STUDIO_FAKE_UNAME: system tools are not probed)"
  else
    info "platform:    macOS ${HOST_MACOS_VERSION} ${HOST_ARCH}"
  fi

  # Source tree
  if [ ! -f "${SOURCE_DIR}/CMakeLists.txt" ] || [ ! -d "${SOURCE_DIR}/Applications/SlicerApp" ]; then
    add_preflight_problem "${SOURCE_DIR} is not a Slicer source tree (use --source DIR)"
  fi

  # Paths: the superbuild does not support spaces, and long paths overflow
  # Mach-O load commands ("malformed mach-o: load commands size").
  case "${SOURCE_DIR}${BUILD_DIR}" in
    *" "*) add_preflight_problem "the source and build paths must not contain spaces (source: '${SOURCE_DIR}', build: '${BUILD_DIR}'); use --build-dir or move the checkout" ;;
  esac
  case "${BUILD_DIR}/" in
    "${SOURCE_DIR}/"*) add_preflight_problem "the build directory must be outside the source tree" ;;
  esac
  case "${SOURCE_DIR}/" in
    "${BUILD_DIR}/"*) add_preflight_problem "the source tree must not be inside the build directory" ;;
  esac
  if [ "${#BUILD_DIR}" -gt 40 ]; then
    warn "the build path is ${#BUILD_DIR} characters long; paths over ~40 characters can break macOS linking (use a short --build-dir such as ~/S)"
  fi

  # macOS version: programs built for the target macOS run during the build.
  if [ -n "${HOST_MACOS_VERSION}" ]; then
    if ! version_ge "${HOST_MACOS_VERSION}" "${MACOS_FLOOR}"; then
      add_preflight_problem "macOS ${MACOS_FLOOR} or newer is required (found ${HOST_MACOS_VERSION})"
    fi
  else
    add_preflight_problem "could not determine the macOS version (sw_vers)"
  fi

  if [ "${FAKED_PLATFORM}" = 0 ] && [ "$(sysctl -in sysctl.proc_translated 2>/dev/null)" = "1" ]; then
    warn "this shell runs under Rosetta 2, so an Intel (x86_64) build will be made; for an Apple Silicon build run: arch -arm64 /bin/bash ${SCRIPT_NAME} ..."
  fi

  # Xcode command line tools
  if [ "${FAKED_PLATFORM}" = 0 ]; then
    if ! xcode-select -p >/dev/null 2>&1 || ! xcrun --find clang >/dev/null 2>&1; then
      add_preflight_problem "the Xcode Command Line Tools are missing: run 'xcode-select --install', then run this script again"
    fi
  else
    info "would check: Xcode Command Line Tools (xcode-select -p, xcrun --find clang)"
  fi

  # Homebrew
  if find_brew; then
    info "homebrew:    ${BREW} (prefix ${BREW_PREFIX})"
  elif [ "${SKIP_DEPS}" = 0 ] && [ "${SKIP_BUILD}" = 0 ]; then
    add_preflight_problem "Homebrew is required to install the build tools. Install it from https://brew.sh with:
      /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"
    then run this script again"
  fi

  # Rosetta 2: the CTK application launcher used by the build tree and by the
  # bundled PythonSlicer is only released as an x86_64 binary.
  if [ "${HOST_ARCH}" = "arm64" ]; then
    if [ "${FAKED_PLATFORM}" = 1 ]; then
      info "would check: Rosetta 2 (needed by the x86_64-only CTK application launcher)"
    elif ! /usr/bin/arch -x86_64 /usr/bin/true >/dev/null 2>&1; then
      if [ "${DRY_RUN}" = 1 ]; then
        info "Rosetta 2 is missing and would be installed (softwareupdate --install-rosetta --agree-to-license)"
      elif confirm "Rosetta 2 is required by the Slicer application launcher. Install it now?"; then
        local sudo_cmd=()
        if [ "$(id -u)" != 0 ]; then
          sudo_cmd=(sudo)
        fi
        run ${sudo_cmd[@]+"${sudo_cmd[@]}"} /usr/sbin/softwareupdate --install-rosetta --agree-to-license ||
          add_preflight_problem "installing Rosetta 2 failed"
      else
        add_preflight_problem "Rosetta 2 is required: run 'softwareupdate --install-rosetta --agree-to-license' (or re-run with --yes)"
      fi
    fi
  fi

  # Disk space
  local free_gb
  free_gb=$(free_disk_gb "${BUILD_DIR}")
  if [ -n "${free_gb}" ]; then
    info "free disk:   ${free_gb} GB at $(existing_ancestor "${BUILD_DIR}")"
    if [ "${free_gb}" -lt "${MIN_FREE_DISK_GB}" ] && [ "${SKIP_BUILD}" = 0 ]; then
      warn "only ${free_gb} GB free; a first build needs about ${MIN_FREE_DISK_GB} GB (sources, build tree, Qt and the package)"
    fi
  fi

  if [ -n "${PREFLIGHT_PROBLEMS}" ]; then
    if [ "${DRY_RUN}" = 1 ]; then
      warn "a real run would stop here:"
      printf '%s' "${PREFLIGHT_PROBLEMS}" >&2
    else
      printf '%s' "${PREFLIGHT_PROBLEMS}" >&2
      die "${EXIT_PREFLIGHT}" "preflight checks failed"
    fi
  fi
}

#-----------------------------------------------------------------------------
# Dependencies
#-----------------------------------------------------------------------------

brew_install() {
  # brew_install <formula>[:<command>]...
  # Install the given formulae when missing. A formula is not needed when its
  # command is already available (for example a ccache installed otherwise).
  # Installed formulae are never upgraded: upgrading Qt underneath an existing
  # build breaks it.
  local missing=() item formula command
  for item in "$@"; do
    formula=${item%%:*}
    command=""
    if [ "${item}" != "${formula}" ]; then
      command=${item#*:}
    fi
    if [ "${FAKED_PLATFORM}" = 0 ] && [ -n "${command}" ] && command -v "${command}" >/dev/null 2>&1; then
      info "${formula}: $(command -v "${command}")"
    elif [ "${FAKED_PLATFORM}" = 0 ] && "${BREW}" list --versions "${formula}" >/dev/null 2>&1; then
      info "${formula}: $("${BREW}" list --versions "${formula}" | head -n 1)"
    else
      missing+=("${formula}")
    fi
  done
  if [ ${#missing[@]} -eq 0 ]; then
    return 0
  fi
  run env HOMEBREW_NO_INSTALL_UPGRADE=1 HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1 HOMEBREW_NO_ENV_HINTS=1 \
    "${BREW}" install "${missing[@]}"
}

homebrew_qt_installed() {
  [ "${FAKED_PLATFORM}" = 0 ] && "${BREW}" list --versions qt >/dev/null 2>&1
}

install_dependencies() {
  section "Build tools"
  if [ "${SKIP_DEPS}" = 1 ]; then
    info "skipped (--skip-deps)"
  else
    local formulae=(git-lfs:git-lfs ccache:ccache)
    if [ "${QT_MODE}" = "homebrew" ]; then
      formulae+=(qt)
      # Homebrew only has prebuilt Qt ("bottles") for Apple Silicon with
      # macOS 15 or newer; elsewhere it compiles Qt.
      if ! homebrew_qt_installed; then
        if [ "${HOST_ARCH}" = "x86_64" ]; then
          warn "Homebrew has no Qt bottles for Intel Macs, so 'brew install qt' compiles Qt from source (hours); --qt=official is much faster"
        elif ! version_ge "${HOST_MACOS_VERSION}" "15"; then
          warn "Homebrew has Qt bottles only for macOS 15 and newer, so on macOS ${HOST_MACOS_VERSION} 'brew install qt' compiles Qt from source (hours); --qt=official is much faster"
        fi
      fi
    fi
    brew_install "${formulae[@]}" || die "${EXIT_DEPS}" "brew install failed"
    run git lfs install --skip-repo || warn "git lfs install failed"
  fi
  setup_cmake
}

cmake_version_of() {
  "$1" --version 2>/dev/null | sed -n 's/^cmake version \([0-9.]*\).*/\1/p' | head -n 1
}

install_pinned_cmake() {
  # install_pinned_cmake <tools dir>
  local tools=$1
  local name="cmake-${PINNED_CMAKE_VERSION}-macos-universal"
  local archive="${tools}/${name}.tar.gz"
  run mkdir -p "${tools}" || return 1
  run curl --fail --location --silent --show-error --retry 3 --output "${archive}" \
    "https://github.com/Kitware/CMake/releases/download/v${PINNED_CMAKE_VERSION}/${name}.tar.gz" || return 1
  if [ "${DRY_RUN}" = 1 ]; then
    info "would check that the SHA-256 of ${archive} is ${PINNED_CMAKE_SHA256}"
  else
    local checksum
    checksum=$(shasum -a 256 "${archive}" | awk '{ print $1 }')
    if [ "${checksum}" != "${PINNED_CMAKE_SHA256}" ]; then
      rm -f "${archive}"
      echo "    unexpected SHA-256 ${checksum} for ${archive}" >&2
      return 1
    fi
  fi
  # Extract next to the final location first, so that an interrupted
  # extraction never leaves an incomplete CMake behind.
  local partial="${tools}/${name}.partial"
  run rm -rf "${partial}" "${tools}/${name}" || return 1
  run mkdir -p "${partial}" || return 1
  run tar -xzf "${archive}" -C "${partial}" || return 1
  run mv "${partial}/${name}" "${tools}/${name}" || return 1
  run rm -rf "${partial}" "${archive}"
}

setup_cmake() {
  # Select the CMake that configures the superbuild, in this order: the pinned
  # CMake downloaded by an earlier run (so a resumed build keeps its CMake), a
  # CMake 3 found on PATH, or else download the pinned CMake.
  local tools="${WORK_DIR}/tools"
  local pinned="${tools}/cmake-${PINNED_CMAKE_VERSION}-macos-universal/CMake.app/Contents/bin/cmake"
  if [ "${FAKED_PLATFORM}" = 1 ]; then
    info "would use a CMake ${MIN_CMAKE_VERSION} to 3.x found on PATH; without one (Homebrew's CMake is 4.x):"
    install_pinned_cmake "${tools}"
    CMAKE_BIN=${pinned}
    return 0
  fi
  local found="" version=""
  if [ -x "${pinned}" ]; then
    found=${pinned}
  else
    found=$(command -v cmake 2>/dev/null || true)
  fi
  if [ -n "${found}" ]; then
    version=$(cmake_version_of "${found}")
  fi
  if [ "${SKIP_DEPS}" = 0 ] && { [ -z "${version}" ] || ! version_ge "${version}" "${MIN_CMAKE_VERSION}" || version_ge "${version}" "4.0"; }; then
    info "found ${found:-no cmake}${version:+ (version ${version})}; using CMake ${PINNED_CMAKE_VERSION} instead"
    install_pinned_cmake "${tools}" || die "${EXIT_DEPS}" "downloading CMake ${PINNED_CMAKE_VERSION} failed"
    CMAKE_BIN=${pinned}
    if [ "${DRY_RUN}" = 1 ]; then
      return 0
    fi
    found=${pinned}
    version=$(cmake_version_of "${found}")
  fi
  if [ -z "${version}" ]; then
    die "${EXIT_DEPS}" "no working cmake found (run without --skip-deps to download CMake ${PINNED_CMAKE_VERSION})"
  fi
  CMAKE_BIN=${found}
  if [ "${CMAKE_BIN}" = "${pinned}" ]; then
    # Also for build steps that run "cmake" by name.
    PATH="$(dirname "${pinned}"):${PATH}"
    export PATH
  fi
  info "cmake ${version} (${CMAKE_BIN})"
  if ! version_ge "${version}" "${MIN_CMAKE_VERSION}"; then
    die "${EXIT_DEPS}" "CMake ${MIN_CMAKE_VERSION} or newer is required (found ${version})"
  fi
  if version_ge "${version}" "4.0"; then
    # CMake 4 rejects projects declaring compatibility with CMake < 3.5; the
    # environment variable reaches every external project of the superbuild.
    export CMAKE_POLICY_VERSION_MINIMUM="${CMAKE_POLICY_VERSION_MINIMUM:-3.5}"
    warn "CMake ${version} is not tested with this source tree (run without --skip-deps to use CMake ${PINNED_CMAKE_VERSION}); setting CMAKE_POLICY_VERSION_MINIMUM=${CMAKE_POLICY_VERSION_MINIMUM}"
  fi
}

#-----------------------------------------------------------------------------
# Qt
#-----------------------------------------------------------------------------

qt_version_of() {
  # qt_version_of <Qt6_DIR or Qt5_DIR>
  local directory=$1 file version
  for file in "${directory}"/Qt6ConfigVersionImpl.cmake "${directory}"/Qt6ConfigVersion.cmake "${directory}"/Qt5ConfigVersion.cmake; do
    if [ -f "${file}" ]; then
      version=$(sed -n 's/^ *set(PACKAGE_VERSION "\([0-9.]*\)").*/\1/p' "${file}" | head -n 1)
      if [ -n "${version}" ]; then
        printf '%s' "${version}"
        return 0
      fi
    fi
  done
  return 1
}

binary_minimum_macos() {
  # Minimum macOS version recorded in a Mach-O binary (LC_BUILD_VERSION).
  if [ ! -e "$1" ] || ! command -v otool >/dev/null 2>&1; then
    return 1
  fi
  local minimum
  minimum=$(otool -arch "${HOST_ARCH}" -l "$1" 2>/dev/null |
    awk '/LC_BUILD_VERSION|LC_VERSION_MIN_MACOSX/ { found = 1 } found && ($1 == "minos" || $1 == "version") { print $2; exit }')
  if [ -z "${minimum}" ]; then
    return 1
  fi
  printf '%s\n' "${minimum}"
}

qt_minimum_macos() {
  # Look next to the CMake package directory, and next to its real location
  # (Homebrew links the Qt CMake files from its "Cellar").
  local config_dir core
  config_dir=$(python3 -c 'import os, sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' \
    "${QT_CMAKE_DIR}/${QT_CMAKE_VAR%_DIR}Config.cmake" 2>/dev/null || true)
  for core in "${QT_CMAKE_DIR}/../../QtCore.framework/QtCore" "${config_dir:-/nonexistent}/../../QtCore.framework/QtCore"; do
    if binary_minimum_macos "${core}"; then
      return 0
    fi
  done
  return 1
}

fingerprint_value() {
  # fingerprint_value <key>: value recorded by the last configuration
  sed -n "s/^$1=//p" "${BUILD_DIR}/${FINGERPRINT_NAME}" 2>/dev/null | head -n 1
}

set_qt_cmake_dir() {
  # set_qt_cmake_dir <candidate>: accepts a Qt prefix or a lib/cmake/Qt6|Qt5 dir
  local candidate=$1
  if [ -f "${candidate}/Qt6Config.cmake" ]; then
    QT_CMAKE_VAR=Qt6_DIR
    QT_CMAKE_DIR=${candidate}
  elif [ -f "${candidate}/lib/cmake/Qt6/Qt6Config.cmake" ]; then
    QT_CMAKE_VAR=Qt6_DIR
    QT_CMAKE_DIR="${candidate}/lib/cmake/Qt6"
  elif [ -f "${candidate}/Qt5Config.cmake" ]; then
    QT_CMAKE_VAR=Qt5_DIR
    QT_CMAKE_DIR=${candidate}
  elif [ -f "${candidate}/lib/cmake/Qt5/Qt5Config.cmake" ]; then
    QT_CMAKE_VAR=Qt5_DIR
    QT_CMAKE_DIR="${candidate}/lib/cmake/Qt5"
  else
    return 1
  fi
  QT_CMAKE_DIR=$(cd "${QT_CMAKE_DIR}" && pwd)
  return 0
}

install_official_qt() {
  local qt_root=$1
  local venv="${WORK_DIR}/tools/aqt-venv"
  local python=""
  if [ -n "${BREW_PREFIX}" ] && [ -x "${BREW_PREFIX}/bin/python3" ]; then
    python="${BREW_PREFIX}/bin/python3"
  else
    python="/usr/bin/python3"
  fi
  if [ ! -x "${venv}/bin/aqt" ]; then
    run "${python}" -m venv "${venv}" || return 1
    run "${venv}/bin/python" -m pip install --quiet --upgrade pip || return 1
    run "${venv}/bin/python" -m pip install --quiet "aqtinstall==${AQTINSTALL_VERSION}" || return 1
  fi
  local modules=()
  read -r -a modules <<<"${OFFICIAL_QT_MODULES}"
  run "${venv}/bin/aqt" install-qt mac desktop "${QT_REQUESTED_VERSION}" clang_64 \
    --modules "${modules[@]}" --outputdir "${qt_root}"
}

resolve_qt() {
  section "Qt (${QT_MODE})"
  local expected=""
  MACOS_MIN=${MACOS_MIN:-${MACOS_FLOOR}}
  case "${QT_MODE}" in
    homebrew)
      info "Homebrew's Qt is built for the macOS version it was bottled for: the DMG will run on this Mac"
      info "and on Macs with the same or a newer macOS. Use --qt=official for a DMG for macOS ${MACOS_FLOOR} and newer."
      local candidate
      for candidate in "${BREW_PREFIX}/lib/cmake/Qt6" "${BREW_PREFIX}/opt/qtbase/lib/cmake/Qt6" "${BREW_PREFIX}/opt/qt/lib/cmake/Qt6"; do
        if set_qt_cmake_dir "${candidate}"; then
          break
        fi
      done
      expected="${BREW_PREFIX}/lib/cmake/Qt6"
      ;;
    official)
      local qt_root="${WORK_DIR}/Qt-official"
      expected="${qt_root}/${QT_REQUESTED_VERSION}/macos/lib/cmake/Qt6"
      if ! set_qt_cmake_dir "${expected}" || [ ! -d "${qt_root}/${QT_REQUESTED_VERSION}/macos/lib/QtWebEngineCore.framework" ]; then
        QT_CMAKE_DIR=""
        if [ "${SKIP_DEPS}" = 1 ]; then
          die "${EXIT_DEPS}" "official Qt ${QT_REQUESTED_VERSION} is not installed in ${qt_root} (run without --skip-deps)"
        fi
        install_official_qt "${qt_root}" || die "${EXIT_DEPS}" "downloading Qt ${QT_REQUESTED_VERSION} with aqtinstall failed"
        set_qt_cmake_dir "${expected}" || true
      fi
      ;;
    source)
      local qt_source_root="${WORK_DIR}/Qt-source"
      local install_dir="${qt_source_root}/qt-everywhere-install-${QT_REQUESTED_VERSION}-${HOST_ARCH}"
      expected="${install_dir}/lib/cmake/Qt6"
      if ! set_qt_cmake_dir "${install_dir}"; then
        if [ "${SKIP_DEPS}" = 1 ]; then
          die "${EXIT_DEPS}" "Qt ${QT_REQUESTED_VERSION} has not been built in ${qt_source_root} (run without --skip-deps)"
        fi
        if [ "${HOST_ARCH}" = "x86_64" ]; then
          warn "Build-qt6.sh expects Homebrew in /opt/homebrew (Apple Silicon); on Intel Macs prefer --qt=official"
        fi
        info "building Qt from source takes several hours and needs the full Xcode (Metal toolchain)"
        run mkdir -p "${qt_source_root}" || die "${EXIT_DEPS}" "could not create ${qt_source_root}"
        info "in ${qt_source_root}:"
        (
          cd "${qt_source_root}" 2>/dev/null || [ "${DRY_RUN}" = 1 ] || exit 1
          run_logged "${LOG_DIR}/qt-source.log" \
            bash "${SOURCE_DIR}/Utilities/Scripts/Build-qt6.sh" "${QT_REQUESTED_VERSION}" "${HOST_ARCH}" "${MACOS_MIN}"
        ) || die "${EXIT_DEPS}" "building Qt from source failed (see ${LOG_DIR}/qt-source.log)"
        set_qt_cmake_dir "${install_dir}" || true
      fi
      ;;
    path)
      set_qt_cmake_dir "${QT_SPEC}" || die "${EXIT_DEPS}" "no Qt6Config.cmake or Qt5Config.cmake found in ${QT_SPEC} (or its lib/cmake/Qt6|Qt5)"
      ;;
  esac

  if [ -z "${QT_CMAKE_DIR}" ]; then
    if [ "${DRY_RUN}" = 1 ]; then
      QT_CMAKE_VAR=Qt6_DIR
      QT_CMAKE_DIR=${expected}
      QT_VERSION_FOUND=${QT_REQUESTED_VERSION:-"<installed Qt version>"}
      info "Qt6_DIR (expected): ${QT_CMAKE_DIR}"
    else
      die "${EXIT_DEPS}" "Qt was not found (expected ${expected})"
    fi
  else
    QT_VERSION_FOUND=$(qt_version_of "${QT_CMAKE_DIR}") || die "${EXIT_DEPS}" "could not read the Qt version in ${QT_CMAKE_DIR}"
    info "${QT_CMAKE_VAR}: ${QT_CMAKE_DIR}"
    info "Qt version: ${QT_VERSION_FOUND}"
  fi

  case "${QT_VERSION_FOUND}" in
    "<"*) QT_MAJOR_MINOR="<Qt major.minor>" ;;
    *)
      QT_MAJOR_MINOR=$(major_minor "${QT_VERSION_FOUND}")
      if [ "${QT_CMAKE_VAR}" = "Qt6_DIR" ] && ! version_ge "${QT_VERSION_FOUND}" "6.8"; then
        die "${EXIT_DEPS}" "Slicer needs Qt 6.8 or newer (found ${QT_VERSION_FOUND})"
      fi
      if [ "${QT_CMAKE_VAR}" = "Qt5_DIR" ] && ! version_ge "${QT_VERSION_FOUND}" "5.15"; then
        die "${EXIT_DEPS}" "Slicer needs Qt 5.15 (found ${QT_VERSION_FOUND})"
      fi
      ;;
  esac

  # The application cannot run on an older macOS than its Qt libraries.
  QT_MINOS=$(qt_minimum_macos 2>/dev/null || true)
  if [ -n "${QT_MINOS}" ]; then
    info "Qt libraries require macOS ${QT_MINOS} or newer"
    if ! version_ge "${MACOS_MIN}" "${QT_MINOS}"; then
      info "raising the minimum macOS version of the build from ${MACOS_MIN} to ${QT_MINOS}"
      MACOS_MIN=${QT_MINOS}
    fi
  fi
  info "minimum macOS of the build (CMAKE_OSX_DEPLOYMENT_TARGET): ${MACOS_MIN}"
  # macOS does not load programs built for a newer macOS, and the build runs
  # the programs it builds. (A macOS older than the source tree's minimum is
  # already reported by the preflight checks.)
  if [ -n "${HOST_MACOS_VERSION}" ] && version_ge "${HOST_MACOS_VERSION}" "${MACOS_FLOOR}" &&
    ! version_ge "${HOST_MACOS_VERSION}" "${MACOS_MIN}"; then
    local problem="this Mac runs macOS ${HOST_MACOS_VERSION}, older than the minimum macOS of the build (${MACOS_MIN}): use a lower --macos-min or a Qt built for this macOS"
    if [ "${DRY_RUN}" = 1 ]; then
      warn "a real run would stop here: ${problem}"
    else
      die "${EXIT_PREFLIGHT}" "${problem}"
    fi
  fi
}

#-----------------------------------------------------------------------------
# Configure and build
#-----------------------------------------------------------------------------

write_fingerprint() {
  printf '%s\n' \
    "# Slicer Studio superbuild configuration (written by ${SCRIPT_NAME})" \
    "source=${SOURCE_DIR}" \
    "arch=${HOST_ARCH}" \
    "qt_dir=${QT_CMAKE_DIR}" \
    "qt_version=${QT_VERSION_FOUND}" \
    "macos_min=${MACOS_MIN}"
}

clean_build_dir() {
  section "Clean"
  if [ ! -d "${BUILD_DIR}" ]; then
    info "nothing to clean: ${BUILD_DIR} does not exist"
    return 0
  fi
  case "${BUILD_DIR}" in
    "" | "/" | "${HOME}" | "${WORK_DIR}" | "${SOURCE_DIR}")
      die "${EXIT_USAGE}" "refusing to delete '${BUILD_DIR}'"
      ;;
  esac
  if [ ! -f "${BUILD_DIR}/CMakeCache.txt" ] && [ ! -f "${BUILD_DIR}/${FINGERPRINT_NAME}" ] && [ -n "$(ls -A "${BUILD_DIR}" 2>/dev/null)" ]; then
    die "${EXIT_USAGE}" "refusing to delete ${BUILD_DIR}: it does not look like a Slicer build directory"
  fi
  if [ "${DRY_RUN}" = 0 ] && ! confirm "Delete the build directory ${BUILD_DIR}?"; then
    die "${EXIT_USAGE}" "not deleting ${BUILD_DIR} (re-run with --yes to confirm)"
  fi
  run rm -rf "${BUILD_DIR}" || die "${EXIT_FAILURE}" "could not delete ${BUILD_DIR}"
}

setup_compilers() {
  # Compiler wrappers that call ccache. Unlike CMAKE_<LANG>_COMPILER_LAUNCHER,
  # the compiler path is forwarded by the superbuild to every external project
  # (VTK, ITK, CTK, Python, ...), so all of them are cached. A later run, for
  # example after --clean, recompiles only what changed.
  local wrapper_dir="${WORK_DIR}/ccache-bin"
  local cc="" cxx="" ccache=""
  if [ "${FAKED_PLATFORM}" = 1 ]; then
    cc="<xcrun --find clang>"
    cxx="<xcrun --find clang++>"
    ccache="${BREW_PREFIX}/bin/ccache"
  else
    cc=$(xcrun --find clang)
    cxx=$(xcrun --find clang++)
    ccache=$(command -v ccache 2>/dev/null || true)
  fi
  if [ -z "${ccache}" ]; then
    warn "ccache not found: building without a compiler cache"
    C_COMPILER=${cc}
    CXX_COMPILER=${cxx}
    return 0
  fi
  C_COMPILER="${wrapper_dir}/clang"
  CXX_COMPILER="${wrapper_dir}/clang++"
  info "compiler cache: ${ccache} (wrappers in ${wrapper_dir})"
  export CCACHE_COMPILERCHECK="${CCACHE_COMPILERCHECK:-content}"
  if [ "${DRY_RUN}" = 1 ]; then
    info "would write ${C_COMPILER}: exec ${ccache} ${cc} \"\$@\""
    info "would write ${CXX_COMPILER}: exec ${ccache} ${cxx} \"\$@\""
    return 0
  fi
  mkdir -p "${wrapper_dir}" || die "${EXIT_CONFIGURE}" "could not create ${wrapper_dir}"
  printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "${ccache}" "${cc}" >"${C_COMPILER}"
  printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "${ccache}" "${cxx}" >"${CXX_COMPILER}"
  chmod 755 "${C_COMPILER}" "${CXX_COMPILER}"
}

configure_superbuild() {
  section "Configure the superbuild"
  setup_compilers

  local fingerprint_file="${BUILD_DIR}/${FINGERPRINT_NAME}"
  local wanted
  wanted=$(write_fingerprint | grep -v '^#')
  if [ -f "${BUILD_DIR}/CMakeCache.txt" ] && [ -f "${BUILD_DIR}/Makefile" ]; then
    if [ -f "${fingerprint_file}" ]; then
      local existing
      existing=$(grep -v '^#' "${fingerprint_file}")
      if [ "${existing}" != "${wanted}" ]; then
        echo "    existing build:" >&2
        printf '%s\n' "${existing}" | sed 's/^/      /' >&2
        echo "    requested:" >&2
        printf '%s\n' "${wanted}" | sed 's/^/      /' >&2
        die "${EXIT_CONFIGURE}" "${BUILD_DIR} was configured with a different source, architecture, Qt or minimum macOS; re-run with --clean (or use another --build-dir)"
      fi
    fi
    if [ "${RECONFIGURE}" = 0 ]; then
      info "already configured: ${BUILD_DIR} (use --reconfigure to run CMake again)"
      return 0
    fi
  fi

  local sysroot="<xcrun --sdk macosx --show-sdk-path>"
  if [ "${FAKED_PLATFORM}" = 0 ]; then
    sysroot=$(xcrun --sdk macosx --show-sdk-path) || die "${EXIT_CONFIGURE}" "could not find the macOS SDK (xcrun --sdk macosx --show-sdk-path)"
  fi

  local args=(
    -S "${SOURCE_DIR}"
    -B "${BUILD_DIR}"
    -G "Unix Makefiles"
    -DCMAKE_BUILD_TYPE:STRING=Release
    -DBUILD_TESTING:BOOL=OFF
    -DSlicer_BUILD_DOCUMENTATION:BOOL=OFF
    -DSlicer_USE_VTK_DEBUG_LEAKS:BOOL=OFF
    -DSlicer_BUILD_QT_DESIGNER_PLUGINS:BOOL=OFF
    "-DCMAKE_OSX_ARCHITECTURES:STRING=${HOST_ARCH}"
    "-DCMAKE_OSX_DEPLOYMENT_TARGET:STRING=${MACOS_MIN}"
    "-DCMAKE_OSX_SYSROOT:PATH=${sysroot}"
    "-D${QT_CMAKE_VAR}:PATH=${QT_CMAKE_DIR}"
    "-DSlicer_REQUIRED_QT_VERSION:STRING=${QT_MAJOR_MINOR}"
    # Packaging embeds Qt (frameworks and plugins) only for a non-system Qt.
    -DSlicer_USE_SYSTEM_QT:BOOL=OFF
    "-DCMAKE_C_COMPILER:FILEPATH=${C_COMPILER}"
    "-DCMAKE_CXX_COMPILER:FILEPATH=${CXX_COMPILER}"
  )
  if [ ${#CMAKE_EXTRA_ARGS[@]} -gt 0 ]; then
    args+=("${CMAKE_EXTRA_ARGS[@]}")
  fi

  run mkdir -p "${BUILD_DIR}" || die "${EXIT_CONFIGURE}" "could not create ${BUILD_DIR}"
  if [ "${DRY_RUN}" = 1 ]; then
    info "would write ${fingerprint_file}"
  else
    write_fingerprint >"${fingerprint_file}"
  fi
  run_logged "${LOG_DIR}/configure.log" "${CMAKE_BIN}" "${args[@]}" ||
    die "${EXIT_CONFIGURE}" "CMake configuration failed (see ${LOG_DIR}/configure.log)"
}

default_jobs() {
  local cpus memory cap
  cpus=$(cpu_count)
  memory=$(memory_gb)
  if [ "${memory}" -gt 0 ]; then
    # Compiling VTK/ITK takes up to ~2 GB per compiler process.
    cap=$((memory / 2))
    if [ "${cap}" -lt 1 ]; then
      cap=1
    fi
    if [ "${cap}" -lt "${cpus}" ]; then
      echo "${cap}"
      return 0
    fi
  fi
  echo "${cpus}"
}

build_superbuild() {
  section "Build (Release superbuild)"
  JOBS=${JOBS:-$(default_jobs)}
  local memory
  memory=$(memory_gb)
  if [ "${memory}" -gt 0 ]; then
    info "jobs: ${JOBS} ($(cpu_count) CPUs, ${memory} GB memory)"
  else
    info "jobs: ${JOBS} ($(cpu_count) CPUs)"
  fi
  info "a first build takes several hours; re-running this script continues where it stopped"
  if [ "${DRY_RUN}" = 0 ] && command -v ccache >/dev/null 2>&1; then
    ccache --zero-stats >/dev/null 2>&1
  fi
  local start=${SECONDS}
  run_logged "${LOG_DIR}/build.log" make -C "${BUILD_DIR}" "-j${JOBS}" ||
    die "${EXIT_BUILD}" "the build failed (see ${LOG_DIR}/build.log; re-run this script to continue)"
  if [ "${DRY_RUN}" = 0 ]; then
    info "build finished in $(format_duration $((SECONDS - start)))"
    if command -v ccache >/dev/null 2>&1; then
      ccache --show-stats 2>/dev/null | sed -n '1,12p' | sed 's/^/    /'
    fi
  fi
}

#-----------------------------------------------------------------------------
# Package
#-----------------------------------------------------------------------------

attach_dmg() {
  # attach_dmg <dmg>: mounts the image read-only and sets ATTACHED_MOUNT.
  # Do not call it in a subshell such as $(...): the mount point must reach
  # MOUNTED_DIRS of this shell, so that cleanup() detaches it on an error or
  # an interruption.
  local dmg=$1 mount_point
  ATTACHED_MOUNT=""
  mount_point=$(mktemp -d "${TMPDIR:-/tmp}/slicer-studio-mount.XXXXXX") || return 1
  if ! hdiutil attach -nobrowse -readonly -noautoopen -noverify -mountpoint "${mount_point}" "${dmg}" </dev/null >/dev/null; then
    rmdir "${mount_point}" 2>/dev/null
    return 1
  fi
  MOUNTED_DIRS="${MOUNTED_DIRS} ${mount_point}"
  ATTACHED_MOUNT=${mount_point}
}

detach_dmg() {
  local mount_point=$1 attempt
  for attempt in 1 2 3; do
    if hdiutil detach "${mount_point}" -quiet >/dev/null 2>&1; then
      break
    fi
    sleep 5
    if [ "${attempt}" = 3 ]; then
      hdiutil detach "${mount_point}" -force -quiet >/dev/null 2>&1
    fi
  done
  rmdir "${mount_point}" 2>/dev/null
  local remaining="" item
  for item in ${MOUNTED_DIRS}; do
    if [ "${item}" != "${mount_point}" ]; then
      remaining="${remaining} ${item}"
    fi
  done
  MOUNTED_DIRS=${remaining}
}

cpack_package() {
  section "Package (CPack)"
  if [ "${DRY_RUN}" = 0 ] && [ ! -f "${SLICER_BUILD_DIR}/CMakeCache.txt" ]; then
    die "${EXIT_PACKAGE}" "${SLICER_BUILD_DIR} does not exist: build first (run without --skip-build)"
  fi
  local marker="${WORK_DIR}/.package-start"
  if [ "${DRY_RUN}" = 0 ]; then
    mkdir -p "${WORK_DIR}" && : >"${marker}"
  fi
  run_logged "${LOG_DIR}/package.log" make -C "${SLICER_BUILD_DIR}" package ||
    die "${EXIT_PACKAGE}" "make package failed (see ${LOG_DIR}/package.log)"

  if [ "${DRY_RUN}" = 1 ]; then
    CPACK_DMG="${SLICER_BUILD_DIR}/Slicer-<version>-macosx-${HOST_ARCH}.dmg"
  else
    CPACK_DMG=$(find "${SLICER_BUILD_DIR}" -maxdepth 1 -name '*.dmg' -newer "${marker}" -print | head -n 1)
    rm -f "${marker}"
    if [ -z "${CPACK_DMG}" ]; then
      die "${EXIT_PACKAGE}" "make package did not produce a .dmg in ${SLICER_BUILD_DIR}"
    fi
  fi
  info "CPack disk image: ${CPACK_DMG}"

  # Check that the bundle is self-contained: every library it loads must be
  # inside the bundle (or part of macOS), not in Homebrew or the build tree.
  if run_logged "${LOG_DIR}/packageverify.log" make -C "${SLICER_BUILD_DIR}" packageverify; then
    PACKAGE_VERIFIED="passed (make packageverify)"
    if [ "${DRY_RUN}" = 1 ]; then
      PACKAGE_VERIFIED="not run (dry run)"
    fi
  else
    PACKAGE_VERIFIED="FAILED"
    warn "the bundle is not self-contained (see ${LOG_DIR}/packageverify.log): it may only run on this Mac"
  fi
}

make_studio_dmg() {
  section "Make the ${APP_NAME} disk image"
  local stage="${WORK_DIR}/dmg-stage-${HOST_ARCH}"
  local app="${stage}/${APP_NAME}.app"
  local plist="${app}/Contents/Info.plist"

  if [ -d "${stage}" ]; then
    run rm -rf "${stage}" || die "${EXIT_PACKAGE}" "could not delete ${stage}"
  fi
  run mkdir -p "${stage}" "${OUT_DIR}" || die "${EXIT_PACKAGE}" "could not create ${stage} or ${OUT_DIR}"

  # Copy the application out of the CPack image under its new name.
  if [ "${DRY_RUN}" = 1 ]; then
    print_cmd hdiutil attach -nobrowse -readonly -noautoopen -noverify -mountpoint "<mount point>" "${CPACK_DMG}"
    print_cmd ditto "<mount point>/<3D Slicer version>.app" "${app}"
    print_cmd hdiutil detach "<mount point>"
  else
    local mount_point source_app
    attach_dmg "${CPACK_DMG}" || die "${EXIT_PACKAGE}" "could not mount ${CPACK_DMG}"
    mount_point=${ATTACHED_MOUNT}
    source_app=$(find "${mount_point}" -maxdepth 1 -name '*.app' -type d -print | head -n 1)
    if [ -z "${source_app}" ]; then
      detach_dmg "${mount_point}"
      die "${EXIT_PACKAGE}" "no application found in ${CPACK_DMG}"
    fi
    info "application in the CPack image: $(basename "${source_app}")"
    run ditto "${source_app}" "${app}" || {
      detach_dmg "${mount_point}"
      die "${EXIT_PACKAGE}" "copying the application failed"
    }
    detach_dmg "${mount_point}"
  fi

  # Rename the application. This must happen before signing: Info.plist is
  # sealed by the code signature. The new identifier keeps macOS (Launch
  # Services, the Dock, privacy permissions) from confusing it with an
  # official Slicer installation.
  if ! {
    run plutil -replace CFBundleName -string "${APP_NAME}" "${plist}" &&
      run plutil -replace CFBundleDisplayName -string "${APP_NAME}" "${plist}" &&
      run plutil -replace CFBundleIdentifier -string "${APP_BUNDLE_ID}" "${plist}" &&
      run plutil -lint "${plist}"
  }; then
    die "${EXIT_PACKAGE}" "updating ${plist} failed"
  fi

  local version="<version>"
  if [ "${DRY_RUN}" = 0 ]; then
    version=$(plutil -extract CFBundleVersion raw -o - "${plist}" 2>/dev/null || true)
    version=$(printf '%s' "${version}" | tr -c 'A-Za-z0-9._-' '-')
    if [ -z "${version}" ]; then
      die "${EXIT_PACKAGE}" "could not read CFBundleVersion from ${plist}"
    fi
    check_bundle "${app}"
    # Let Finder refuse to start the application on a too old macOS instead
    # of failing to load its libraries.
    MACOS_MIN=$(binary_minimum_macos "${app}/Contents/MacOS/SlicerApp-real" 2>/dev/null || true)
    if [ -n "${MACOS_MIN}" ]; then
      run plutil -replace LSMinimumSystemVersion -string "${MACOS_MIN}" "${plist}" ||
        die "${EXIT_PACKAGE}" "updating ${plist} failed"
    fi
  else
    print_cmd plutil -replace LSMinimumSystemVersion -string "<minimum macOS of SlicerApp-real>" "${plist}"
  fi

  # Sign: rewriting Info.plist invalidated the bundle signature.
  if [ "${NO_SIGN}" = 1 ]; then
    warn "--no-sign: '${APP_NAME}.app' has an invalid signature until you sign it"
  else
    run xattr -cr "${app}"
    local sign_args=(--app "${app}" --identity "${SIGN_IDENTITY}")
    local signer="${SOURCE_DIR}/Utilities/Scripts/SlicerSignBundleMacOS.py"
    if [ -f "${signer}" ]; then
      # Inside-out signing (each Mach-O, then the bundle): more reliable than
      # "codesign --deep" on a bundle of this size.
      run python3 "${signer}" "${sign_args[@]}" || die "${EXIT_PACKAGE}" "code signing failed"
    else
      run codesign --force --deep --sign "${SIGN_IDENTITY}" "${app}" || die "${EXIT_PACKAGE}" "code signing failed"
    fi
    if ! run codesign --verify --deep --strict "${app}"; then
      # The strict check also reports distribution concerns (for example
      # unusual files in the bundle) that do not prevent the application from
      # starting; the bundle's own signature must be valid, though.
      run codesign --verify "${app}" || die "${EXIT_PACKAGE}" "the code signature of ${app} does not verify"
      warn "strict code signature verification failed (see above); the application signature itself is valid"
    fi
  fi

  run ln -s /Applications "${stage}/Applications" || die "${EXIT_PACKAGE}" "could not create the Applications link"

  FINAL_DMG="${OUT_DIR}/${DMG_PREFIX}-${version}-macos-${HOST_ARCH}.dmg"
  local size="<application size + 15%>m"
  if [ "${DRY_RUN}" = 0 ]; then
    rm -f "${FINAL_DMG}" "${FINAL_DMG}.sha256"
    local size_mb
    size_mb=$(du -sm "${stage}" | awk '{ print $1 }')
    # Explicit size: hdiutil's own estimate can be too small for a big bundle.
    size="$((size_mb * 115 / 100 + 100))m"
  fi
  local attempt created=0
  for attempt in 1 2 3; do
    if run hdiutil create -volname "${APP_NAME}" -srcfolder "${stage}" -size "${size}" \
      -fs HFS+ -format "${DMG_FORMAT}" -ov "${FINAL_DMG}"; then
      created=1
      break
    fi
    # hdiutil intermittently fails with "Resource busy" while the system scans
    # freshly written files.
    warn "hdiutil create failed (attempt ${attempt} of 3)"
    sleep 15
  done
  if [ "${created}" = 0 ]; then
    die "${EXIT_PACKAGE}" "could not create ${FINAL_DMG}"
  fi
  run hdiutil verify "${FINAL_DMG}" || die "${EXIT_PACKAGE}" "${FINAL_DMG} does not verify"
  if [ "${DRY_RUN}" = 0 ]; then
    (cd "${OUT_DIR}" && shasum -a 256 "$(basename "${FINAL_DMG}")" >"$(basename "${FINAL_DMG}").sha256")
    rm -rf "${stage}"
  fi
  info "created ${FINAL_DMG}"
}

check_bundle() {
  local app=$1
  local contents="${app}/Contents"
  local real="${contents}/MacOS/SlicerApp-real"
  [ -x "${contents}/MacOS/Slicer" ] || die "${EXIT_PACKAGE}" "missing ${contents}/MacOS/Slicer"
  [ -e "${contents}/lib/QtPlugins/platforms/libqcocoa.dylib" ] ||
    die "${EXIT_PACKAGE}" "the Qt platform plugin (lib/QtPlugins/platforms/libqcocoa.dylib) is missing: the application could not start"
  if [ ! -e "${contents}/lib/QtPlugins/iconengines/libqsvgicon.dylib" ]; then
    warn "the Qt SVG icon engine plugin is not bundled (lib/QtPlugins/iconengines): SVG icons are rendered from scaled pixmaps"
  fi
  if [ -e "${real}" ] && command -v lipo >/dev/null 2>&1; then
    local archs
    archs=$(lipo -archs "${real}" 2>/dev/null || true)
    case " ${archs} " in
      *" ${HOST_ARCH} "*) info "application binary: ${archs}" ;;
      *) die "${EXIT_PACKAGE}" "${real} is built for '${archs}', expected ${HOST_ARCH}" ;;
    esac
  fi
}

#-----------------------------------------------------------------------------
# Start-up test
#-----------------------------------------------------------------------------

smoke_test() {
  section "Start-up test"
  local dmg=${SMOKE_DMG:-${FINAL_DMG}}
  if [ -z "${dmg}" ] && [ "${DRY_RUN}" = 0 ]; then
    local candidate
    for candidate in "${OUT_DIR}/${DMG_PREFIX}"-*-macos-"${HOST_ARCH}".dmg; do
      if [ -f "${candidate}" ] && { [ -z "${dmg}" ] || [ "${candidate}" -nt "${dmg}" ]; }; then
        dmg=${candidate}
      fi
    done
  fi
  if [ "${DRY_RUN}" = 0 ] && [ ! -f "${dmg}" ]; then
    die "${EXIT_SMOKE}" "no ${DMG_PREFIX} disk image found in ${OUT_DIR} (use --dmg PATH)"
  fi
  dmg=${dmg:-"${OUT_DIR}/${DMG_PREFIX}-<version>-macos-${HOST_ARCH}.dmg"}
  info "disk image: ${dmg}"

  # Runs the real start-up (main window, Studio shell, modules, Python, saving
  # and reloading a scene and a NIfTI file) from the read-only disk image with
  # temporary settings, so the user's Slicer.ini (shared with official Slicer
  # installations) is not touched. The check is shared with the Linux package
  # build (Utilities/Scripts/SlicerStudioSmokeCheck.py); the inline fallback
  # only covers a copy of this script used outside the source tree.
  local shared_check="${SOURCE_DIR}/Utilities/Scripts/SlicerStudioSmokeCheck.py"
  local code
  if [ -f "${shared_check}" ]; then
    code=$(cat "${shared_check}")
  else
    code=$(
      cat <<'PY'
import qt
import slicer
mainWindow = slicer.util.mainWindow()
modules = [name for name in ("volumes", "segmenteditor", "markups", "data") if not hasattr(slicer.modules, name)]
import numpy
import ssl
print("SLICER_STUDIO_SMOKE version=%s qt=%s mainWindow=%s studio=%s" % (
    slicer.app.applicationVersion, qt.qVersion(), mainWindow is not None, hasattr(slicer.modules, "studio")))
if mainWindow is None or modules:
    raise RuntimeError("start-up test failed: main window %s, missing modules %s" % (mainWindow, modules))
print("SLICER_STUDIO_SMOKE_OK")
PY
    )
  fi
  local python_code="import sys, numpy, ssl; print('PythonSlicer', sys.version.split()[0])"
  local log="${LOG_DIR}/smoke-${HOST_ARCH}.log"
  # Multi-line code cannot be given with --python-code (the command line
  # parser rejects it), so the check is run as a script.
  local check_script="${LOG_DIR}/smoke-check.py"
  if [ "${DRY_RUN}" = 1 ]; then
    print_cmd hdiutil attach -nobrowse -readonly -noautoopen -noverify -mountpoint "<mount point>" "${dmg}"
    print_cmd codesign --verify --deep --strict "<mount point>/${APP_NAME}.app"
    info "would write ${check_script}"
    print_cmd env SLICER_DISABLE_STARTUP_PREWARM=1 "<mount point>/${APP_NAME}.app/Contents/MacOS/Slicer" \
      --no-splash --disable-settings --ignore-slicerrc --exit-after-startup --python-script "${check_script}"
    print_cmd "<mount point>/${APP_NAME}.app/Contents/bin/PythonSlicer" -c "${python_code}"
    print_cmd hdiutil detach "<mount point>"
    return 0
  fi

  mkdir -p "${LOG_DIR}"
  : >"${log}"
  printf '%s\n' "${code}" >"${check_script}"
  local mount_point app status=0
  attach_dmg "${dmg}" || die "${EXIT_SMOKE}" "could not mount ${dmg}"
  mount_point=${ATTACHED_MOUNT}
  app="${mount_point}/${APP_NAME}.app"
  if [ ! -d "${app}" ]; then
    detach_dmg "${mount_point}"
    die "${EXIT_SMOKE}" "${APP_NAME}.app not found in ${dmg}"
  fi
  local identifier
  identifier=$(plutil -extract CFBundleIdentifier raw -o - "${app}/Contents/Info.plist" 2>/dev/null || true)
  info "bundle identifier: ${identifier}"
  if [ "${identifier}" != "${APP_BUNDLE_ID}" ]; then
    status=1
    echo "    unexpected bundle identifier (expected ${APP_BUNDLE_ID})"
  fi
  if ! codesign --verify --deep --strict "${app}" >>"${log}" 2>&1; then
    warn "the code signature does not verify (see ${log})"
  fi

  print_cmd env SLICER_DISABLE_STARTUP_PREWARM=1 "${app}/Contents/MacOS/Slicer" \
    --no-splash --disable-settings --ignore-slicerrc --exit-after-startup --python-script "${check_script}"
  if ! run_with_timeout 600 env SLICER_DISABLE_STARTUP_PREWARM=1 "${app}/Contents/MacOS/Slicer" \
    --no-splash --disable-settings --ignore-slicerrc --exit-after-startup --python-script "${check_script}" >>"${log}" 2>&1; then
    status=1
  fi
  if ! grep -q "SLICER_STUDIO_SMOKE_OK" "${log}"; then
    status=1
  fi
  # Errors in start-up callbacks (for example while the Studio shell is
  # applied) are printed but do not stop the application.
  if grep -q "^Traceback (most recent call last)" "${log}"; then
    status=1
    echo "    a Python error was reported during start-up"
  fi
  grep "SLICER_STUDIO_SMOKE " "${log}" | sed 's/^/    /'

  # PythonSlicer starts the application's own Python (used for background
  # processing); it is an x86_64 launcher, run through Rosetta 2 on Apple Silicon.
  print_cmd "${app}/Contents/bin/PythonSlicer" -c "${python_code}"
  if ! run_with_timeout 180 "${app}/Contents/bin/PythonSlicer" -c "${python_code}" >>"${log}" 2>&1; then
    status=1
    echo "    PythonSlicer failed"
  fi
  grep "^PythonSlicer " "${log}" | sed 's/^/    /'
  detach_dmg "${mount_point}"

  if [ "${status}" != 0 ]; then
    echo "    last 80 lines of ${log}:"
    tail -n 80 "${log}" | sed 's/^/    | /'
    die "${EXIT_SMOKE}" "the start-up test failed (see ${log})"
  fi
  info "start-up test passed"
}

#-----------------------------------------------------------------------------
# Main
#-----------------------------------------------------------------------------

print_summary() {
  section "Done"
  if [ -n "${FINAL_DMG}" ]; then
    info "disk image:       ${FINAL_DMG}"
    if [ "${DRY_RUN}" = 0 ]; then
      info "size:             $(du -h "${FINAL_DMG}" | awk '{ print $1 }')"
      info "sha256:           $(awk '{ print $1 }' "${FINAL_DMG}.sha256")"
    fi
    info "bundle check:     ${PACKAGE_VERIFIED}"
    info "minimum macOS:    ${MACOS_MIN:-<unknown>}"
    info "Qt:               ${QT_VERSION_FOUND:-$(fingerprint_value qt_version)}"
    echo ""
    info "Install: open the disk image and drag '${APP_NAME}' to Applications."
    info "It is ad-hoc signed, not notarized: on first start macOS blocks it; allow it in"
    info "System Settings > Privacy & Security > Open Anyway, or run:"
    info "  xattr -dr com.apple.quarantine '/Applications/${APP_NAME}.app'"
  elif [ "${NO_PACKAGE}" = 1 ]; then
    info "build complete in ${BUILD_DIR} (packaging skipped with --no-package)"
  fi
}

main() {
  parse_args "$@"
  setup_colors
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  detect_platform
  resolve_locations
  if [ "${DRY_RUN}" = 1 ]; then
    echo "Dry run: printing the plan; nothing is installed, built or written."
  fi

  if [ "${SMOKE_ONLY}" = 1 ]; then
    smoke_test
    return 0
  fi

  resolve_minimum_macos
  preflight
  if [ "${SKIP_BUILD}" = 0 ]; then
    install_dependencies
    resolve_qt
    if [ "${DO_CLEAN}" = 1 ]; then
      clean_build_dir
    fi
    configure_superbuild
    build_superbuild
  fi
  if [ "${NO_PACKAGE}" = 0 ]; then
    cpack_package
    make_studio_dmg
    if [ "${NO_SMOKE}" = 0 ]; then
      smoke_test
    fi
  fi
  print_summary
}

main "$@"

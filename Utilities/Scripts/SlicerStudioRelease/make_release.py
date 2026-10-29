#!/usr/bin/env python3
r"""Prepare the files of a Slicer Studio release (A-08, decision D-U1).

Slicer Studio updates itself from the public GitHub releases of this repository. A release is created and
uploaded BY HAND by the publisher; this script only prepares the files and prints the steps. It never contacts
GitHub (no API, no token, no upload).

It writes ``studio-update.json`` (the manifest that the application reads) with the size and SHA-256 of every
package, and the web launcher bundle (``SlicerStudioWeb-<version>.zip``) when a Linux package is given::

    python3 make_release.py ~/SlicerStudio/dist/SlicerStudio-1.0.0-macos-arm64.dmg \
        ~/Slicer-SuperBuild/Slicer-build/Slicer-5.13.0-linux-amd64.tar.gz --notes notes.md

    python3 make_release.py --required --minimum-supported 0.9.0 --notes-text "Fixes a data loss bug." PACKAGES...

The platform of each package is recognized from its name (``*-macos-arm64.dmg``, ``*-macos-x86_64.dmg``,
``*-linux-amd64.tar.gz``); ``--asset KEY=PATH`` names it explicitly. The version is ``STUDIO_VERSION`` of
``Modules/Scripted/Studio/StudioLib/version.py`` (``--version`` must match it). Only the Python standard library
is used (Python 3.8 or later). See README.md next to this script.
"""

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SOURCE_DIR = SCRIPT_DIR.parent.parent.parent
VERSION_FILE = SOURCE_DIR / "Modules" / "Scripted" / "Studio" / "StudioLib" / "version.py"
WEB_DIR = SOURCE_DIR / "Utilities" / "SlicerStudioWeb"
WEB_FILES = ("slicer_studio_web.py", "Dockerfile", "README.md", "docker")

REPOSITORY = "azhmeerjesani/Slicer-2"
MANIFEST_NAME = "studio-update.json"
PLATFORM_KEYS = ("macos-arm64", "macos-x86_64", "linux-x86_64", "web")
#: Names that GitHub keeps as they are (spaces would become dots) and that the application accepts
ASSET_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$")


class ReleaseError(Exception):
    """Reported without a traceback."""


def read_studio_version(path=VERSION_FILE):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^STUDIO_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def version_key(text):
    match = VERSION_PATTERN.match(text)
    if not match:
        raise ReleaseError(f"{text!r} is not a version such as 1.2.0")
    pre = match.group(4)
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre[1:].split(".")) if pre else ()
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), 0 if pre else 1, parts)


def platform_of(path):
    """The manifest key of a package, from its file name, or None."""
    name = Path(path).name.lower()
    if name.endswith(".dmg"):
        if "arm64" in name or "aarch64" in name:
            return "macos-arm64"
        if "x86_64" in name or "x86-64" in name or "intel" in name:
            return "macos-x86_64"
        return None
    if name.endswith((".tar.gz", ".tgz")) and "linux" in name and ("amd64" in name or "x86_64" in name):
        return "linux-x86_64"
    if name.endswith(".zip") and "web" in name:
        return "web"
    return None


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def linux_package_version(path):
    """STUDIO_VERSION inside a Linux package, or None (read without extracting the package)."""
    try:
        with tarfile.open(path, "r:*") as archive:
            for member in archive:
                if member.isfile() and member.name.endswith("qt-scripted-modules/StudioLib/version.py"):
                    stream = archive.extractfile(member)
                    match = re.search(rb'^STUDIO_VERSION\s*=\s*"([^"]+)"', stream.read(), re.MULTILINE) if stream else None
                    return match.group(1).decode() if match else None
    except (OSError, tarfile.TarError, EOFError) as error:
        raise ReleaseError(f"{path} cannot be read: {error}")
    return None


def make_web_bundle(version, out_dir):
    """Zip the web launcher folder (with a VERSION file) as SlicerStudioWeb-<version>.zip. Return its path."""
    path = Path(out_dir) / f"SlicerStudioWeb-{version}.zip"
    top = f"SlicerStudioWeb-{version}"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in WEB_FILES:
            source = WEB_DIR / name
            if not source.exists():
                raise ReleaseError(f"{source} is missing")
            files = sorted(p for p in source.rglob("*") if p.is_file()) if source.is_dir() else [source]
            for file in files:
                if "__pycache__" in file.parts:
                    continue
                info = zipfile.ZipInfo(f"{top}/{file.relative_to(WEB_DIR).as_posix()}", date_time=(2020, 1, 1, 0, 0, 0))
                mode = 0o755 if file.suffix in (".py", ".sh") else 0o644
                info.external_attr = (0o100000 | mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                bundle.writestr(info, file.read_bytes())
        info = zipfile.ZipInfo(f"{top}/VERSION", date_time=(2020, 1, 1, 0, 0, 0))
        info.external_attr = (0o100644) << 16
        bundle.writestr(info, version + "\n")
    return path


def git_commit():
    try:
        result = subprocess.run(["git", "-C", str(SOURCE_DIR), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def check_published(text):
    """--published must be a real calendar date written YYYY-MM-DD."""
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    try:
        if not match:
            raise ValueError(text)
        datetime.date(*(int(part) for part in match.groups()))
    except ValueError:
        raise ReleaseError(f"--published {text!r} is not a date YYYY-MM-DD (for example 2026-10-22).") from None
    return text


def build_manifest(version, assets, required=False, minimum_supported=None, notes="", published=None):
    manifest = {
        "version": version,
        "required": bool(required),
        "minimumSupported": minimum_supported,
        "published": published or datetime.date.today().isoformat(),
        "notes": notes,
        "assets": {},
    }
    for key in PLATFORM_KEYS:
        if key in assets:
            path = Path(assets[key])
            manifest["assets"][key] = {"name": path.name, "size": path.stat().st_size, "sha256": sha256_of(path)}
    return manifest


def collect_assets(args):
    assets = {}
    for path in args.packages:
        key = platform_of(path)
        if key is None:
            raise ReleaseError(f"The platform of {Path(path).name} is not recognized; use --asset KEY=PATH (KEY: {', '.join(PLATFORM_KEYS)}).")
        if key in assets:
            raise ReleaseError(f"Two packages for {key}: {Path(assets[key]).name} and {Path(path).name}.")
        assets[key] = path
    for item in args.asset or []:
        key, separator, path = item.partition("=")
        if not separator or key not in PLATFORM_KEYS:
            raise ReleaseError(f"--asset expects KEY=PATH with KEY one of {', '.join(PLATFORM_KEYS)}, got {item!r}.")
        assets[key] = path
    for key, path in assets.items():
        file = Path(path)
        if not file.is_file():
            raise ReleaseError(f"{path} does not exist.")
        if not ASSET_NAME_PATTERN.match(file.name) or ".." in file.name:
            raise ReleaseError(f"Rename {file.name}: release file names may only contain letters, digits, '.', '_', '+' and '-'.")
    names = [Path(path).name for path in assets.values()]
    if len(set(names)) != len(names):
        raise ReleaseError("Two release files have the same name.")
    return {key: str(Path(path).resolve()) for key, path in assets.items()}


def print_steps(version, manifest_path, files, required, notes_file):
    tag = f"v{version}"
    commit = git_commit()
    print()
    print(f"Release files for Slicer Studio {version} are ready. Nothing was uploaded: publish the release by hand.")
    print()
    print(f"1. Open https://github.com/{REPOSITORY}/releases/new in your web browser (signed in to GitHub).")
    print(f"2. Choose a tag: type {tag} and choose \"Create new tag: {tag} on publish\".")
    if commit:
        print(f"   Target: the commit these packages were built from ({commit[:12]}), if it is on GitHub; else the branch you build from.")
    print(f"3. Release title: Slicer Studio {version}")
    print("   Description: the release notes" + (f" ({notes_file})" if notes_file else "") + ".")
    print("4. Attach these files (drag them onto \"Attach binaries\"); keep their names unchanged:")
    for path in files:
        print(f"     {path}")
    print("5. Leave \"Set as a pre-release\" unchecked and check \"Set as the latest release\". Publish release.")
    print(f"6. Check: https://github.com/{REPOSITORY}/releases/latest/download/{MANIFEST_NAME} opens this manifest.")
    print()
    if required:
        print("This release is REQUIRED: older installed versions show a blocking 'Update required' dialog at their next start (when they check for updates).")
    else:
        print("This release is optional: Slicer Studio offers it in Settings > Updates and with a notice that can be skipped.")
    print(f"Manifest: {manifest_path}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare the files of a Slicer Studio release (nothing is uploaded).",
                                     epilog="See README.md next to this script.")
    parser.add_argument("packages", nargs="*", metavar="PACKAGE", help="built packages: *-macos-arm64.dmg, *-macos-x86_64.dmg, *-linux-amd64.tar.gz")
    parser.add_argument("--asset", action="append", metavar="KEY=PATH", help=f"a package with its platform key ({', '.join(PLATFORM_KEYS)})")
    parser.add_argument("--version", help="version of the release (default: STUDIO_VERSION of StudioLib/version.py; must match it)")
    parser.add_argument("--required", action="store_true", help="mark the release as required (older versions must update at startup)")
    parser.add_argument("--minimum-supported", metavar="VERSION", help="versions older than this must update at startup")
    notes = parser.add_mutually_exclusive_group()
    notes.add_argument("--notes", metavar="FILE", help="release notes (plain text or Markdown file)")
    notes.add_argument("--notes-text", metavar="TEXT", help="release notes as text")
    parser.add_argument("--published", metavar="YYYY-MM-DD", help="publication date (default: today)")
    parser.add_argument("--no-web", action="store_true", help="do not make the web launcher bundle")
    parser.add_argument("--out-dir", metavar="DIR", help="where to write the manifest and the web bundle (default: release-v<version>)")
    parser.add_argument("--allow-version-mismatch", action="store_true", help="accept a Linux package whose STUDIO_VERSION differs")
    args = parser.parse_args(argv)
    try:
        source_version = read_studio_version()
        version = args.version or source_version
        if not version:
            raise ReleaseError(f"No version: {VERSION_FILE} was not found; use --version.")
        if version.startswith("v"):
            version = version[1:]
        version_key(version)
        if source_version and version != source_version:
            raise ReleaseError(f"--version {version} differs from STUDIO_VERSION {source_version} in {VERSION_FILE}: "
                               "raise STUDIO_VERSION and build the packages again.")
        if args.published is not None:
            check_published(args.published)
        if args.minimum_supported:
            version_key(args.minimum_supported)
            if version_key(args.minimum_supported) > version_key(version):
                raise ReleaseError(f"--minimum-supported {args.minimum_supported} is newer than the release {version}.")
        assets = collect_assets(args)
        if not assets:
            raise ReleaseError("Give at least one package (see --help).")
        if "linux-x86_64" in assets:
            inside = linux_package_version(assets["linux-x86_64"])
            if inside is None:
                print(f"Warning: no StudioLib/version.py found in {Path(assets['linux-x86_64']).name}.", file=sys.stderr)
            elif inside != version and not args.allow_version_mismatch:
                raise ReleaseError(f"{Path(assets['linux-x86_64']).name} contains Slicer Studio {inside}, not {version}.")
        for key in ("macos-arm64", "macos-x86_64"):
            if key in assets and version not in Path(assets[key]).name:
                print(f"Warning: {Path(assets[key]).name} does not have {version} in its name (was it built from this version?).", file=sys.stderr)
        missing = [key for key in PLATFORM_KEYS if key != "web" and key not in assets]
        if (args.required or args.minimum_supported) and missing:
            print(f"Warning: this required release has no package for {', '.join(missing)}. Installations there cannot update: "
                  "they show a warning (no blocking dialog) until the release has their package. Add the packages to the "
                  "release, and upload the manifest, before or together with them.", file=sys.stderr)
        notes_text = ""
        if args.notes:
            try:
                notes_text = Path(args.notes).read_text(encoding="utf-8")
            except UnicodeDecodeError as error:
                raise ReleaseError(f"The notes file {args.notes} is not UTF-8 text ({error.reason} at byte {error.start}): "
                                   "save it as UTF-8.") from None
            except OSError as error:
                raise ReleaseError(f"Cannot read the notes file {args.notes}: {error.strerror or error}.") from None
        elif args.notes_text:
            notes_text = args.notes_text
        out_dir = Path(args.out_dir or f"release-v{version}").resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        if "linux-x86_64" in assets and "web" not in assets and not args.no_web:
            assets["web"] = str(make_web_bundle(version, out_dir))
        manifest = build_manifest(version, assets, args.required, args.minimum_supported, notes_text, args.published)
        manifest_path = out_dir / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        files = [assets[key] for key in PLATFORM_KEYS if key in assets] + [str(manifest_path)]
        print_steps(version, manifest_path, files, args.required, args.notes)
    except ReleaseError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

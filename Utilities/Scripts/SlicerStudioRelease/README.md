# Publish a Slicer Studio release

Slicer Studio updates itself from the public releases of this repository
(<https://github.com/azhmeerjesani/Slicer-2/releases>). You create each release by
hand on the GitHub website. `make_release.py` prepares the files and prints the
steps; it never contacts GitHub (no API, no token, no upload).

## What the application reads

At startup (and once a day while it runs), an installed Slicer Studio downloads one
small file:

```
https://github.com/azhmeerjesani/Slicer-2/releases/latest/download/studio-update.json
```

This is GitHub's public download address of the **latest** release, so the release
must be marked as the latest one. The file is the release manifest:

```json
{
  "version": "1.2.0",
  "required": false,
  "minimumSupported": null,
  "published": "2026-10-22",
  "notes": "Release notes (plain text or Markdown).",
  "assets": {
    "macos-arm64": {"name": "SlicerStudio-1.2.0-macos-arm64.dmg", "size": 1234567, "sha256": "…"},
    "linux-x86_64": {"name": "Slicer-5.13.0-linux-amd64.tar.gz", "size": 1234567, "sha256": "…"},
    "web": {"name": "SlicerStudioWeb-1.2.0.zip", "size": 12345, "sha256": "…"}
  }
}
```

- An update is **optional** unless you mark it **required** (`--required`) or set a
  **minimum supported version** (`--minimum-supported 1.1.0`: older versions must
  update). Optional updates appear as a notice that users can skip and in Settings >
  Updates. Required ones show a blocking "Update required" dialog at startup with
  *Update now* and *Quit*, on the platforms whose package the release has; elsewhere
  they show a warning only. So attach the packages of every platform to a required
  release (the script warns when one is missing).
- The application downloads the file of its platform from
  `https://github.com/azhmeerjesani/Slicer-2/releases/download/v<version>/<name>` and
  installs it only if its size and SHA-256 match the manifest. So the tag must be
  `v<version>` and the file names must stay as they are.

## Steps

1. Raise `STUDIO_VERSION` in `Modules/Scripted/Studio/StudioLib/version.py` (for
   example to `1.2.0`) and commit it.
2. Build the packages from that commit:
   - macOS: `Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh` on each Mac
     (`SlicerStudio-1.2.0-macos-arm64.dmg`, `…-macos-x86_64.dmg`);
   - Linux: `make package` in the build tree (`Slicer-<Slicer version>-linux-amd64.tar.gz`).
3. Prepare the release files:

   ```console
   python3 Utilities/Scripts/SlicerStudioRelease/make_release.py \
       ~/SlicerStudio/dist/SlicerStudio-1.2.0-macos-arm64.dmg \
       /path/to/Slicer-build/Slicer-5.13.0-linux-amd64.tar.gz \
       --notes notes.md
   ```

   It writes `release-v1.2.0/studio-update.json` (sizes and SHA-256 of the packages)
   and, with a Linux package, the web launcher bundle `SlicerStudioWeb-1.2.0.zip`,
   then prints the steps below with the exact file list.
4. On <https://github.com/azhmeerjesani/Slicer-2/releases/new>: tag `v1.2.0` (created
   on publish), title `Slicer Studio 1.2.0`, the notes as description, attach every
   listed file (packages, web bundle and `studio-update.json`), leave *pre-release*
   unchecked, check *Set as the latest release*, and publish.
5. Open
   <https://github.com/azhmeerjesani/Slicer-2/releases/latest/download/studio-update.json>:
   it must show the new manifest.

## Options

| Option | Meaning |
|---|---|
| `PACKAGE …` | Packages; the platform comes from the name (`*-macos-arm64.dmg`, `*-macos-x86_64.dmg`, `*-linux-amd64.tar.gz`) |
| `--asset KEY=PATH` | A package with an explicit platform key (`macos-arm64`, `macos-x86_64`, `linux-x86_64`, `web`) |
| `--version X.Y.Z` | Must equal `STUDIO_VERSION` (default) |
| `--required` | Every older version must update at its next start |
| `--minimum-supported X.Y.Z` | Versions older than X.Y.Z must update |
| `--notes FILE` / `--notes-text TEXT` | Release notes shown in the application |
| `--published YYYY-MM-DD` | Publication date (default today) |
| `--no-web` | Do not make the web launcher bundle |
| `--out-dir DIR` | Output folder (default `release-v<version>`) |

The script checks that the version is valid, that the Linux package contains the
same `STUDIO_VERSION`, that file names have no spaces or unusual characters (GitHub
would rename them), and that `--minimum-supported` is not newer than the release.

## Mistakes and corrections

- Wrong file uploaded: delete the asset on the release page, upload the right one and
  upload a new `studio-update.json` made by `make_release.py` from the right files.
  A file whose checksum does not match the manifest is refused by the application;
  nothing is installed.
- To withdraw a release, mark the previous release as the latest one again (or delete
  the release). Versions never go backwards: an installed newer version is kept.
- Development builds (build trees) do not check by themselves and never install
  updates; Settings > Updates > *Check now* shows the latest release.

# Notes for Claude Code (Slicer Studio fork)

## GitHub: version control only

- Use GitHub only as the git remote: `git push`, `git pull`, `git fetch`. Push only the working branch;
  do not push tags.
- Never use GitHub Actions. Every workflow file runs only from a manual "Run workflow"
  (`workflow_dispatch`): do not add a push, pull request, tag or schedule trigger, do not add a workflow
  file, and do not trigger, re-run or re-enable a workflow. The two workflows that ran (`codespell`,
  `Slicer Studio packages`) are also disabled on the repository; keep them that way.
- Do not use any other GitHub feature: no API calls (`api.github.com`, `gh`, GitHub MCP tools), pull
  requests, issues, releases, packages (`ghcr.io`), Git LFS, Codespaces or artifacts.
- Build packages locally instead. The macOS disk image is built on the user's Mac with
  `Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh`, and the Linux package with `make package` in
  the build tree (see `Docs/user_guide/install_studio.md`).

## Nothing that costs money

The user pays for any usage beyond their plans, so even an accidental use costs them money.

- Do not use paid or metered services: cloud command-line tools (`gcloud`, `gsutil`, `bq`, `aws`),
  container registries, or connectors and MCP tools for other services (Adobe, Dropbox, Google Drive,
  Gmail, Google Calendar, Claude Docs, Slack and others), unless the user asks for that service.
- Do not create cloud sessions, routines (scheduled triggers) or webhooks unless the user asks.
- Download only from free public sources (Python packages, upstream Slicer data, template sites),
  never from the user's own GitHub releases, packages or artifacts.
- When a Claude usage limit is reached, stop and report; do not work around it.

## Agent concurrency

- Run at most one agent at any time, day and night: one standalone agent, or one workflow whose agents
  run strictly one after another (no `parallel()` or concurrent `pipeline()` stages). Wait for it to
  finish before starting the next.
- Use the cheapest model that does the job well: Haiku for mechanical edits, Sonnet for routine
  implementation, docs, tests and walkthroughs, Opus for design, C++, numerics, file compatibility
  and adversarial review. Use Fable when it is really needed: a second fix after a fix failed its
  recheck, reviews of statistics or file-compatibility work, and crashes that Opus could not explain.

## Standing rules

- Files saved by Slicer Studio must open in official 3D Slicer, and files saved by official 3D Slicer
  must open in Slicer Studio.
- Files meant to be shared (scenes, `.mrb`, sidecars, exported tables) must not contain absolute local
  paths: store paths relative to the file, or a portable identifier, and keep local paths in fork-only
  settings. Provenance and reproduce scripts are the exception, by design.
- Settings are shared with official installations (`Slicer.ini`): write fork-only keys under
  `Studio/*` or `Appearance/*`, and never rewrite official keys.
- Never bundle or automatically download connectomes (for example GSP1000). The app may download one
  only when the user asks for it ("Get a connectome…") and has accepted the provider's live terms text;
  sources without stated terms are links only (approved by the user, decision D-L1). Templates are
  downloaded only when the user asks.
- Code must work with Qt 5.15 (Linux build) and Qt 6 (macOS disk image). PythonQt differs between
  them: use the helpers in `Modules/Scripted/Studio/StudioLib/shell.py` (for example
  `associatedWidgets()`), `obj.isWidgetType()` rather than `isinstance(obj, qt.QWidget)`, and
  `property()`/`setProperty()` for QFlags properties.

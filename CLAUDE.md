# Notes for Claude Code (Slicer Studio fork)

## GitHub: version control only

- Use GitHub only as the git remote: `git push`, `git pull`, `git fetch`.
- Never use GitHub Actions. Do not trigger, re-run or re-enable a workflow, and do not push a change
  that starts one, such as `.github/studio-packages-request.txt` or a `studio-build-*` tag. Both
  workflows (`codespell`, `Slicer Studio packages`) are disabled on the repository; keep them that way.
- Do not use any other GitHub feature: no API calls (`api.github.com`, `gh`, GitHub MCP tools),
  pull requests, issues, releases, packages or artifacts.
- Build packages locally instead. The macOS disk image is built on the user's Mac with
  `Utilities/Scripts/SlicerStudioMacOS/build_dmg.sh`, and the Linux package with `make package` in
  the build tree (see `Docs/user_guide/install_studio.md`).

## Agent concurrency (user's time zone: US Eastern)

- From 11 pm to 6 am: any number of agents and workflows may run at once.
- From 6 am to 11 pm: at most 2 agents running at any time (one workflow, which runs at most 2 agents,
  or up to 2 standalone agents). Wind night work down before 6 am; do not stop work mid-agent if it
  can be avoided, start nothing new that would exceed the limit.
- Use the cheapest model that does the job well: Haiku for mechanical edits, Sonnet for routine
  implementation, docs, tests and walkthroughs, Opus for design, C++, numerics, file compatibility
  and adversarial review.

## Standing rules

- Files saved by Slicer Studio must open in official 3D Slicer, and files saved by official 3D Slicer
  must open in Slicer Studio.
- Settings are shared with official installations (`Slicer.ini`): write fork-only keys under
  `Studio/*` or `Appearance/*`, and never rewrite official keys.
- Never bundle or automatically download connectomes (for example GSP1000). Templates are downloaded
  only when the user asks.
- Code must work with Qt 5.15 (Linux build) and Qt 6 (macOS disk image). PythonQt differs between
  them: use the helpers in `Modules/Scripted/Studio/StudioLib/shell.py` (for example
  `associatedWidgets()`), `obj.isWidgetType()` rather than `isinstance(obj, qt.QWidget)`, and
  `property()`/`setProperty()` for QFlags properties.

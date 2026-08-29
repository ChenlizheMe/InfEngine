# Contributing to Infernux

Thanks for contributing.

## Before you start

- Read the main `README.md` for project scope and current limitations.
- Search existing issues and discussions before opening a new thread.
- Keep changes focused. Mixed refactors and feature work are much harder to review in an engine codebase.

## Local setup

The repository provides one Conda environment definition for the supported
development ABI, Python 3.13:

```powershell
./scripts/setup/configure_development.ps1
conda activate infernux
cmake --preset windows-msvc-release
cmake --build --preset windows-msvc-wheel
```

On Linux, run `bash scripts/setup/configure_development.sh`. These setup scripts
initialize submodules and create or repair the `infernux` environment from
`environment.yml`. CMake intentionally rejects another Python minor version so
a local build cannot silently produce an incompatible wheel.

For Hub development:

```bash
conda activate infernux
python packaging/launcher.py
```

## Workspace output layout

Generated files have one canonical home:

- `out/build/<preset>/` contains CMake configure and build trees.
- `out/package/` contains disposable Hub and installer staging output.
- `dist/releases/<version>/` contains final, upload-ready release assets only.
- `dev/` contains private plans and drafts; it is not a build-output directory.

Do not create new top-level `build-*`, `release-*`, or package-output directories.
Run `./scripts/maintenance/clean_workspace.ps1` from PowerShell to remove all
disposable output while preserving `dist/releases` and `dev`. Repository-level
automation is indexed in `scripts/README.md`; website-only tools remain under
`docs/tools/`.

## What to include in a change

- A clear problem statement.
- The smallest practical implementation that solves it at the root cause.
- Updates to docs when public APIs, workflows, or user-facing behavior change.
- Validation notes in the PR describing what you built, ran, or manually verified.

## Validation expectations

The right validation depends on what you changed:

- Python API or tooling changes: run targeted Python tests or static validation.
- Native runtime changes: build the relevant CMake targets and describe runtime checks.
- Docs and website changes: regenerate generated docs when the API surface changed.

Documentation regeneration:

```bash
conda activate infernux
scripts\docs\update_api_docs.bat
```

## Pull request guidance

- Explain the problem first, then the implementation.
- Call out behavior changes, migration impact, and follow-up work explicitly.
- Include screenshots for editor, Hub, or website changes when relevant.
- If a change is intentionally incomplete, say so directly.

## Coding guidelines

- Preserve existing style within the touched area.
- Avoid unrelated cleanup unless it is required to make the change correct.
- Prefer explicit ownership and readable control flow over clever abstractions.
- Do not check in generated binaries or local environment artifacts.

## Discussions and questions

Use the [Infernux community](https://infernux-engine.discourse.group/) for open-ended design conversations or evaluation questions. Use Issues for actionable bugs, feature requests, and task-shaped work.

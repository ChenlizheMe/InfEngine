# Contributing to Infernux

Thanks for contributing.

## Before you start

- Read the main `README.md` for project scope and current limitations.
- Search existing issues and discussions before opening a new thread.
- Keep changes focused. Mixed refactors and feature work are much harder to review in an engine codebase.

## Local setup

The most common workflow in this repository uses Conda:

```bash
conda create -n infengine python=3.12 -y
conda activate infengine
pip install -r requirements.txt
cmake --preset release
cmake --build --preset release
```

For Hub development:

```bash
conda activate infengine
python packaging/launcher.py
```

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
conda activate infengine
python docs/wiki/generate_api_docs.py
python -m mkdocs build --clean -f docs/wiki/mkdocs.yml
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

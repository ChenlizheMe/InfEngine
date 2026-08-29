# Repository automation

All repository-level maintenance entry points live under `scripts/`. Product
runtime code stays in `cpp/` and `python/`; Hub application code stays in
`packaging/`; website-specific generators and contract tests stay in
`docs/tools/`.

| Directory | Purpose | Primary entry point |
|:----------|:--------|:--------------------|
| `acceptance/` | Reusable project-level runtime acceptance and cross-host trajectory comparison | `headless_project_smoke.py` / `compare_headless_trajectories.py` |
| `build/` | Build wrappers needed by a specific host toolchain | `cmake_build.py` |
| `docs/` | Maintainer entry points that orchestrate documentation tools | `update_api_docs.bat` |
| `maintenance/` | Safe local workspace housekeeping | `clean_workspace.ps1` |
| `release/` | Hub, installer, wheel, and GitHub Release orchestration | `release_hub.bat` |
| `setup/` | Clone bootstrap and the supported Python 3.13 Conda environment | `configure_development.ps1` / `configure_development.sh` |

Run every command from the repository root. The entry points resolve the root
from their own location, so they also work when invoked by an absolute path.

Generated files follow the repository output policy:

- `out/` is disposable build, packaging, test, and diagnostic output.
- `dist/releases/<version>/` contains final local release artifacts.
- `dev/` contains private planning and publication drafts and is never cleaned.

Website tool names under `docs/tools/` are intentionally verb-based:
`build-*` creates deterministic artifacts, `check-*` enforces a contract,
`test-*` exercises browser-independent behavior, and `verify-site.mjs` is the
aggregate consistency gate.

For cross-host physics evidence, run `headless_project_smoke.py` with identical
`--fixed-delta`, `--play-frames`, `--track-object`, and `--sample-every`
arguments on each host. Save each result with `--trajectory-output`, then pass
the two JSON files to `compare_headless_trajectories.py`. The comparison ignores
host-specific project paths and checks the sampled state with an explicit
numeric tolerance.

Browser acceptance dependencies are isolated under `scripts/acceptance/`.
Run `npm ci --prefix scripts/acceptance` after cloning, then invoke
`web_mobile_input_smoke.cjs` against a locally served Web Player. On Windows it
uses the installed Microsoft Edge binary; CI hosts may install the pinned
Playwright Chromium build explicitly.

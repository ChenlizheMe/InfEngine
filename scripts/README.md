# Repository automation

All repository-level maintenance entry points live under `scripts/`. Product
runtime code stays in `cpp/` and `python/`; Hub application code stays in
`packaging/`; website-specific generators and contract tests stay in
`docs/tools/`.

| Directory | Purpose | Primary entry point |
|:----------|:--------|:--------------------|
| `build/` | Build wrappers needed by a specific host toolchain | `cmake_build.py` |
| `docs/` | Maintainer entry points that orchestrate documentation tools | `update_api_docs.bat` |
| `maintenance/` | Safe local workspace housekeeping | `clean_workspace.ps1` |
| `release/` | Hub, installer, wheel, and GitHub Release orchestration | `release_hub.bat` |

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

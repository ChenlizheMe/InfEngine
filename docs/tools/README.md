# Website build tools

This directory contains deterministic website generation and validation tools.
They are intentionally kept beside the static site they maintain. The
repository-level maintainer entry point is
`scripts/docs/update_api_docs.bat`.

## Naming contract

| Prefix | Responsibility | May update checked-in output |
|:-------|:---------------|:-----------------------------|
| `build-*` | Generate a deterministic website artifact | Yes; most support `--check` |
| `apply-*` / `normalize-*` / `optimize-*` / `stamp-*` | Transform or normalize one documented part of the site | Yes; check mode where applicable |
| `check-*` | Enforce a static, deployment, performance, or accessibility contract | No, except an explicitly requested report file |
| `test-*` | Exercise browser-independent website behavior and regressions | No |
| `verify-site.mjs` | Aggregate repository, release, page, and metadata consistency checks | No |

`i18n-source.json` is the source of truth for route-localized text.
`build-i18n.mjs` produces the checked-in `docs/js/i18n*.js` bundles; do not edit
those bundles by hand. `normalize-i18n-fallbacks.mjs` rewrites the inline English
inside every `[data-i18n]` element to match that same source, so the pre-script
and crawler view of a page never drifts from the localized copy.

The GitHub workflows in `.github/workflows/build-wiki.yml` and
`.github/workflows/website-quality.yml` define the authoritative execution
order. When adding a new generator, add its check to the quality workflow and
document whether it mutates a committed artifact.

## Release versions

The current website, API baseline, Hub catalog, and engine use the same version
from `pyproject.toml`. Keep older release notes, download options, and API snapshots
as history; do not relabel their artifacts.

Generate `release.json` and `hub-catalog.json` from the versioned Windows/Linux
distribution directory with `python scripts/release/build_release_catalog.py
--release-dir dist/releases/<version>`. The optional `--linux-inventory` accepts
a verified CI archive inventory (`files` mapping archive paths to byte sizes,
plus its parsed Hub `manifest`). Artifact filenames and sizes come from those
inputs, not from the previous release.

Before publication, `published_at` is null and current-release downloads stay
disabled. After uploading the matching GitHub Release assets, rerun the generator
with its actual `--published-at` timestamp, regenerate release notes and the
Service Worker, and deploy the catalogs. Changing version metadata alone does
not upload binaries or publish a release.

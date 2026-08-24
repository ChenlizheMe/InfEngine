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
those bundles by hand.

The GitHub workflows in `.github/workflows/build-wiki.yml` and
`.github/workflows/website-quality.yml` define the authoritative execution
order. When adding a new generator, add its check to the quality workflow and
document whether it mutates a committed artifact.

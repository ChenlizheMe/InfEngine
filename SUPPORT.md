# Support

## Platform support

The [support matrix](docs/platform-support.json) is the shared source for the
English/Chinese README tables and wheel OS classifiers. A classifier identifies
a host build target, not a claim that its 0.4.0 release gates have closed.

Windows x64 is the published 0.3.7 baseline. Linux Editor/Player and Android/Web
Players remain **0.4.0 development targets**. macOS, native iOS and a Headless
Player package are not supported. Headless is a Windows/Linux host mode;
iPhone/iPad browser testing concerns the Web Player, not a native iOS export.
Torch and model execution remain outside the 0.4.0 default build.

For the exact commit `22464e91cc4adec874dc04a5aae0baad849d5437`, the
[desktop CI](https://github.com/ChenlizheMe/Infernux/actions/runs/33950440144)
passed both host suites and built their wheel/Hub distributions, and the
[Player CI](https://github.com/ChenlizheMe/Infernux/actions/runs/33950440171)
passed Windows, Linux, Android, Web build and Web browser jobs. These public
results do not certify an arbitrary later commit, a clean graphical installation,
two arm64 device tiers, mobile Safari, or mixed-DPI displays. Those require their
own acceptance evidence before announcing full platform support.

The website's [Hub catalog](docs/hub-catalog.json) independently records artifacts
that have actually been published. An unpublished Linux download must stay
unavailable; CI artifacts are not silently promoted to public releases.

## Documentation first

Start with these resources:

- `README.md` for project scope, setup, and build instructions
- `README-zh.md` for the Chinese overview
- `docs/wiki.html` for the documentation hub
- `docs/wiki/site/` for generated API reference output

## Where to ask what

- Bug report: open a GitHub Issue with reproduction details.
- Feature proposal: open a GitHub Issue describing the workflow gap and proposed direction.
- Open-ended question or design discussion: use the [Infernux community](https://infernux-engine.discourse.group/).
- Security concern: follow `SECURITY.md` and report it privately.

## What helps when asking for support

Include as much of the following as possible:

- engine version or commit hash
- OS and Python version
- compiler / toolchain information
- relevant logs or screenshots
- a minimal reproduction project or script

The easier it is to reproduce your problem, the faster it is to reason about it.

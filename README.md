<p align="center">
  <img src="docs/assets/logo.png" alt="Infernux logo" width="128" />
</p>

<h1 align="center">Infernux · 熔炉</h1>

<p align="center">
  <strong>C++ / Vulkan runtime. Python is the real development interface.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/version-0.4.0--dev-orange.svg" alt="Version 0.4.0 development" />
  <img src="https://img.shields.io/badge/status-active_development-yellow.svg" alt="Active development" />
  <img src="https://img.shields.io/badge/current_platform-Windows-lightgrey.svg" alt="Current platform: Windows" />
  <img src="https://img.shields.io/badge/python-3.13-brightgreen.svg" alt="Python 3.13" />
  <img src="https://img.shields.io/badge/graphics-Vulkan-red.svg" alt="Vulkan" />
</p>

<p align="center">
  <a href="README-zh.md">简体中文</a> ·
  <a href="https://infernux-engine.com/">Website</a> ·
  <a href="https://infernux-engine.com/wiki.html">Documentation</a> ·
  <a href="https://infernux-engine.discourse.group/">Forum</a> ·
  <a href="https://github.com/ChenlizheMe/Infernux/releases">Releases</a>
</p>

<p align="center">
  <img src="docs/assets/demo.png" alt="Infernux editor rendering a 65,536-object voxel scene with a custom RenderStack" width="100%" />
</p>

This is a real editor capture from the 0.3.4 showcase: 65,536 ordinary GameObjects, one mesh, one material, and a RenderStack doing lighting, fog, color, tilt-shift, and MSAA.

## What this is

A general-purpose game engine. Not a chat box glued onto someone else's editor.

The runtime is C++17 and Vulkan. Gameplay, components, editor tools, assets, and render setup are written in Python 3.13.

This development tree targets **0.4.0**. Windows x64 is stable; Android and Web Players are being validated on the same project. Linux Player support and the PyTorch stack are not ready yet.

## What you can do now

- Scenes, components, physics, audio, UI, animation, particles, prefabs
- Vulkan Forward / Forward+ / Deferred, PBR, RenderGraph, RenderStack
- Hub, installer, and standalone Windows Player builds
- Plugins, same idea as Unity packages

MCP is no longer welded into the engine. It is the official default plugin `infernux/mcp`. New projects include it. Turn it off or uninstall it if you do not want it.

In 0.3.7, animation-only FBX files can drive a matching skinned model without geometrically guessing joint correspondence; Assimp pivot helpers are handled, while incompatible rigs fail explicitly.

## Plugins

An Infernux plugin is an InxPackage. Drop a `.inxpkg`, point at a folder, paste a GitHub URL, or install from the official list.

```
MyPlugin/
  InxPackage.json
  README.md
  Runtime/          # ships with the game
  Editor/           # Editor only
  InxPluginPages/   # extra tabs in the Plugins window
```

Runtime code and regular assets go into a Player build. Editor scripts stay in the Editor.

[Plugin guide](https://infernux-engine.com/wiki/site/en/plugin-package-content.html)

## Get started

Download the Windows x64 installer from [GitHub Releases](https://github.com/ChenlizheMe/Infernux/releases/latest) and let InfernuxHub manage engine versions.

From source you need Windows 10/11 x64, Python 3.13, Vulkan SDK 1.3+, CMake 3.25+, Visual Studio 2022, and MSVC v143:

```powershell
git clone --recurse-submodules https://github.com/ChenlizheMe/Infernux.git
cd Infernux
./scripts/setup/configure_development.ps1
conda activate infernux
cmake --preset windows-msvc-release
cmake --build --preset windows-msvc-wheel
python packaging/launcher.py
```

On Linux, run `bash scripts/setup/configure_development.sh` instead. The setup
script initializes submodules and creates the repository's Python 3.13 Conda
environment from `environment.yml`. If an older `infernux` environment uses a
different Python ABI, the script replaces it instead of trying to reuse it.

```bash
python -m pytest python/test/ -v
cmake --preset windows-msvc-dev
cmake --build --preset windows-msvc-dev
ctest --preset windows-msvc-dev --output-on-failure
```

## Docs

- [Documentation](https://infernux-engine.com/wiki.html)
- [API](https://infernux-engine.com/wiki/site/en/api/index.html)
- [Plugins](https://infernux-engine.com/wiki/site/en/plugin-package-content.html)
- [Release notes](UpdateLog.md)
- [Roadmap](https://infernux-engine.com/roadmap.html)
- [Paper](https://arxiv.org/pdf/2604.10263)

## Citation

```bibtex
@software{chen2026infernux,
  author  = {Chen, Lizhe},
  title   = {Infernux},
  year    = {2026},
  version = {0.4.0},
  url     = {https://github.com/ChenlizheMe/Infernux}
}
```

## License

MIT. See [LICENSE](LICENSE). Bug reports should include the engine version, OS, and how to reproduce. Read [CONTRIBUTING.md](CONTRIBUTING.md) before sending a large change.

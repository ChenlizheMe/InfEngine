#!/usr/bin/env bash
set -euo pipefail

if ! command -v apt-get >/dev/null 2>&1; then
    echo "This helper currently supports Ubuntu and Debian systems with apt-get." >&2
    exit 1
fi

sudo apt-get update
sudo apt-get install --yes --no-install-recommends \
    build-essential \
    ccache \
    clang \
    clang-format \
    git \
    glslang-tools \
    libasound2-dev \
    libdecor-0-dev \
    libdrm-dev \
    libffi-dev \
    libgbm-dev \
    libpipewire-0.3-dev \
    libpulse-dev \
    libudev-dev \
    libusb-1.0-0-dev \
    libvulkan-dev \
    libwayland-dev \
    libx11-dev \
    libxcursor-dev \
    libxext-dev \
    libxfixes-dev \
    libxi-dev \
    libxkbcommon-dev \
    libxrandr-dev \
    libxrender-dev \
    libxss-dev \
    libxtst-dev \
    lld \
    llvm \
    mesa-vulkan-drivers \
    ninja-build \
    pkg-config \
    libspirv-cross-c-shared-dev \
    spirv-tools \
    vulkan-validationlayers \
    vulkan-tools \
    xauth \
    xvfb \
    zlib1g-dev

echo "Linux native build dependencies are installed. No reboot is required."

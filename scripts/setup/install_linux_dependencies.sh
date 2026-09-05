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
    libegl1 \
    libffi-dev \
    libgbm-dev \
    libgl1 \
    libpipewire-0.3-dev \
    libpulse-dev \
    libudev-dev \
    libusb-1.0-0-dev \
    libvulkan-dev \
    libwayland-dev \
    libx11-dev \
    libx11-xcb1 \
    libxcb1 \
    libxcb-cursor0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-shm0 \
    libxcb-sync1 \
    libxcb-util1 \
    libxcb-xfixes0 \
    libxcb-xkb1 \
    libxcursor-dev \
    libxext-dev \
    libxfixes-dev \
    libxi-dev \
    libxkbcommon-dev \
    libxkbcommon-x11-0 \
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

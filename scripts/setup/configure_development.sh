#!/usr/bin/env bash
set -euo pipefail

repository_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repository_root"

command -v git >/dev/null 2>&1 || {
    echo "Git is required to configure the Infernux source tree." >&2
    exit 1
}
command -v conda >/dev/null 2>&1 || {
    echo "Conda is required. Install Miniforge, Miniconda, or Anaconda first." >&2
    exit 1
}

git submodule update --init --recursive

if conda env list | awk '$1 == "infernux" { found = 1 } END { exit !found }'
then
    conda env update -n infernux -f environment.yml --prune
else
    conda env create -f environment.yml
fi

eval "$(conda shell.bash hook)"
conda activate infernux
python -c 'import sys; assert sys.version_info[:2] == (3, 13), sys.version'

echo "Infernux development environment is ready."
echo "Next: cmake --preset linux-clang-release"
echo "Then: cmake --build --preset linux-clang-wheel"

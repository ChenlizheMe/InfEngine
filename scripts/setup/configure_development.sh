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

if conda env list | awk '$1 == "infernux" { found = 1 } END { exit !found }'; then
    environment_python="$(conda run -n infernux python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null | awk '/^[[:space:]]*[0-9]+\.[0-9]+[[:space:]]*$/ { value=$1 } END { print value }')"
    if [[ "$environment_python" != "3.13" ]]; then
        if [[ "${CONDA_DEFAULT_ENV:-}" == "infernux" ]]; then
            eval "$(conda shell.bash hook)"
            for _depth in {1..8}; do
                [[ "${CONDA_DEFAULT_ENV:-}" != "infernux" ]] && break
                conda deactivate
            done
            if [[ "${CONDA_DEFAULT_ENV:-}" == "infernux" ]]; then
                echo "Deactivate the infernux Conda environment, then run this setup script again." >&2
                exit 1
            fi
        fi
        echo "Replacing the incompatible infernux Conda environment with Python 3.13."
        conda env remove -n infernux --yes
        conda env create -f environment.yml
    else
        conda env update -n infernux -f environment.yml --prune
    fi
else
    conda env create -f environment.yml
fi

eval "$(conda shell.bash hook)"
conda activate infernux
python -c 'import sys; assert sys.version_info[:2] == (3, 13), sys.version'

echo "Infernux development environment is ready."
echo "Next: cmake --preset linux-clang-release"
echo "Then: cmake --build --preset linux-clang-wheel"

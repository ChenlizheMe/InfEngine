$ErrorActionPreference = "Stop"

$RepositoryRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $RepositoryRoot
try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is required to configure the Infernux source tree."
    }
    if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
        throw "Conda is required. Install Miniforge, Miniconda, or Anaconda first."
    }

    git submodule update --init --recursive
    if ($LASTEXITCODE -ne 0) {
        throw "Git submodules could not be initialized."
    }

    $EnvironmentNames = conda env list --json | ConvertFrom-Json
    $HasEnvironment = $EnvironmentNames.envs | Where-Object {
        (Split-Path $_ -Leaf) -eq "infernux"
    }
    if ($HasEnvironment) {
        conda env update -n infernux -f environment.yml --prune
    } else {
        conda env create -f environment.yml
    }
    if ($LASTEXITCODE -ne 0) {
        throw "The infernux Conda environment could not be prepared."
    }

    (& conda "shell.powershell" "hook") | Out-String | Invoke-Expression
    conda activate infernux
    python -c "import sys; assert sys.version_info[:2] == (3, 13), sys.version"
    if ($LASTEXITCODE -ne 0) {
        throw "The infernux environment is not using Python 3.13."
    }

    Write-Host "Infernux development environment is ready."
    Write-Host "Next: cmake --preset windows-msvc-release"
    Write-Host "Then: cmake --build --preset windows-msvc-wheel"
} finally {
    Pop-Location
}

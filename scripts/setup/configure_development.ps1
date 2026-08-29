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
    $RecreateEnvironment = $false
    if ($HasEnvironment) {
        $EnvironmentPythonOutput = & conda run -n infernux python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        $CondaRunExitCode = $LASTEXITCODE
        $EnvironmentPython = @(
            $EnvironmentPythonOutput | Where-Object { $_ -match '^\s*\d+\.\d+\s*$' }
        ) | Select-Object -Last 1
        $EnvironmentPython = if ($EnvironmentPython) { $EnvironmentPython.Trim() } else { "" }
        $RecreateEnvironment = $CondaRunExitCode -ne 0 -or $EnvironmentPython -ne "3.13"
    }

    if ($RecreateEnvironment) {
        if ($env:CONDA_DEFAULT_ENV -eq "infernux") {
            (& conda "shell.powershell" "hook") | Out-String | Invoke-Expression
            for ($Depth = 0; $Depth -lt 8 -and $env:CONDA_DEFAULT_ENV -eq "infernux"; $Depth++) {
                conda deactivate
            }
            if ($env:CONDA_DEFAULT_ENV -eq "infernux") {
                throw "Deactivate the infernux Conda environment, then run this setup script again."
            }
        }
        Write-Host "Replacing the incompatible infernux Conda environment with Python 3.13."
        conda env remove -n infernux --yes
        if ($LASTEXITCODE -ne 0) {
            throw "The incompatible infernux Conda environment could not be removed."
        }
        conda env create -f environment.yml
    } elseif ($HasEnvironment) {
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

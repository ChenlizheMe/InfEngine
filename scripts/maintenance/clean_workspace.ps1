[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$LegacyStagingOnly
)

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$RootPrefix = $Root.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar

function Remove-GeneratedPath([string]$Path) {
    $Resolved = [IO.Path]::GetFullPath($Path)
    if (-not $Resolved.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the workspace: $Resolved"
    }
    if (-not (Test-Path -LiteralPath $Resolved)) { return }
    if ($PSCmdlet.ShouldProcess($Resolved, 'Remove generated workspace output')) {
        Remove-Item -LiteralPath $Resolved -Recurse -Force
    }
}

# A Windows staging root passed verbatim into WSL can be materialized as a
# malformed, repository-local directory (for example a Unicode-escaped form
# of ``C:\_InxBuild``). It is always generated output. Resolve candidates from
# the workspace itself and pass every removal through the same containment
# check as the canonical output roots.
$LegacyStagingRoots = @(
    Get-ChildItem -LiteralPath $Root -Directory -Force -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name.EndsWith('_InxBuild', [StringComparison]::OrdinalIgnoreCase) -or
            $_.Name.EndsWith('_InfBuild', [StringComparison]::OrdinalIgnoreCase)
        }
)
foreach ($LegacyStagingRoot in $LegacyStagingRoots) {
    Remove-GeneratedPath $LegacyStagingRoot.FullName
}
if ($LegacyStagingOnly) {
    Write-Host 'Legacy repository-local staging outputs cleaned.' -ForegroundColor Green
    return
}

# Canonical policy:
#   out/                    disposable build, package, test, and diagnostic output
#   dist/releases/<version> final local release archives (preserved here)
#   dev/                    private plans and drafts (preserved here)
$BlockedPaths = [Collections.Generic.List[string]]::new()

$GeneratedRoots = @(
    (Join-Path $Root 'build'),
    (Join-Path $Root 'packaging\runtime'),
    (Join-Path $Root 'packaging\Nuitka'),
    (Join-Path $Root 'packaging\nuitka-crash-report.xml'),
    (Join-Path $Root 'python\Infernux.egg-info'),
    (Join-Path $Root 'pytest_run.log'),
    (Join-Path $Root '.pytest_cache'),
    (Join-Path $Root '__pycache__')
)
foreach ($GeneratedRoot in $GeneratedRoots) {
    try {
        Remove-GeneratedPath $GeneratedRoot
    } catch [UnauthorizedAccessException] {
        $BlockedPaths.Add([IO.Path]::GetFullPath($GeneratedRoot))
    }
}

$PythonCaches = @(Get-ChildItem -LiteralPath $Root -Directory -Recurse -Force -Filter '__pycache__' -ErrorAction SilentlyContinue)
foreach ($PythonCache in $PythonCaches) {
    try {
        Remove-GeneratedPath $PythonCache.FullName
    } catch [UnauthorizedAccessException] {
        $BlockedPaths.Add([IO.Path]::GetFullPath($PythonCache.FullName))
    }
}

# Remove out/ one direct child at a time. This still clears all ordinary output
# if a single stale test directory has a broken Windows ACL.
$OutRoot = Join-Path $Root 'out'
Get-ChildItem -LiteralPath $OutRoot -Force -ErrorAction SilentlyContinue |
    ForEach-Object {
        $ChildPath = $_.FullName
        try {
            Remove-GeneratedPath $ChildPath
        } catch [UnauthorizedAccessException] {
            $BlockedPaths.Add([IO.Path]::GetFullPath($ChildPath))
        }
    }
if ((Test-Path -LiteralPath $OutRoot) -and -not (Get-ChildItem -LiteralPath $OutRoot -Force -ErrorAction SilentlyContinue)) {
    Remove-GeneratedPath $OutRoot
}

$LegacyDistEntries = @(
    (Join-Path $Root 'dist\release'),
    (Join-Path $Root 'dist\Infernux Hub'),
    (Join-Path $Root 'dist\installer')
)
foreach ($LegacyEntry in $LegacyDistEntries) {
    try {
        Remove-GeneratedPath $LegacyEntry
    } catch [UnauthorizedAccessException] {
        $BlockedPaths.Add([IO.Path]::GetFullPath($LegacyEntry))
    }
}

Get-ChildItem -LiteralPath (Join-Path $Root 'dist') -Force -ErrorAction SilentlyContinue |
    Where-Object { -not $_.PSIsContainer -and $_.Extension -in @('.whl', '.tmp') } |
    ForEach-Object { Remove-GeneratedPath $_.FullName }

Get-ChildItem -LiteralPath (Join-Path $Root 'dist') -Directory -Force -ErrorAction SilentlyContinue |
    Where-Object { $_.Name.StartsWith('.tmp-', [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Remove-GeneratedPath $_.FullName }

if ($BlockedPaths.Count -gt 0) {
    $BlockedList = $BlockedPaths -join ', '
    throw "Workspace cleanup completed except for paths with broken access control: $BlockedList"
}

Write-Host 'Workspace outputs cleaned. dist/releases and dev were preserved.' -ForegroundColor Green

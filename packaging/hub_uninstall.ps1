param(
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [switch]$Quiet
)
$ErrorActionPreference = 'Stop'
try {
    if ($ParentPid -gt 0) {
        $parent = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
        if ($parent) { $parent.WaitForExit() }
    }
    $root = (Resolve-Path -LiteralPath $InstallDir).ProviderPath
    $markerName = '.infernux-hub-install.json'
    $marker = Get-Content -LiteralPath (Join-Path $root $markerName) -Raw | ConvertFrom-Json
    if ($marker.tool -ne 'Infernux Hub' -or $marker.kind -ne 'install-directory') {
        throw 'Uninstaller requires a marked Hub installation'
    }
    if ((Test-Path -LiteralPath (Join-Path $root 'Assets') -PathType Container) -and
        (Test-Path -LiteralPath (Join-Path $root 'ProjectSettings') -PathType Container)) {
        throw 'Uninstaller cannot remove a project directory'
    }
    $targets = @()
    foreach ($entry in Get-ChildItem -LiteralPath $root -Force) {
        if ($entry.Name -eq $markerName) { continue }
        if ($entry.Name -eq 'InfernuxHubData') {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Hub data directory must not redirect outside the installation'
            }
            $targets += @(Get-ChildItem -LiteralPath $entry.FullName -Force | Where-Object Name -ne 'Shared')
        } else {
            $targets += $entry
        }
    }
    function Remove-ApplicationEntry($entry) {
        $target = [IO.Path]::GetFullPath($entry.FullName)
        if (-not $target.StartsWith($root.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Uninstall target is outside the installation: $target"
        }
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            if ($entry.PSIsContainer) { [IO.Directory]::Delete($target) }
            else { [IO.File]::Delete($target) }
        } elseif ($entry.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $target -Force) {
                Remove-ApplicationEntry $child
            }
            [IO.Directory]::Delete($target)
        } else {
            Remove-Item -LiteralPath $target -Force
        }
    }
    foreach ($entry in $targets) { Remove-ApplicationEntry $entry }
    if (-not $Quiet) {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show("Hub application removed. Shared resources are preserved at:`n$root\InfernuxHubData\Shared", 'Infernux Hub') | Out-Null
    }
    exit 0
} catch {
    if (-not $Quiet) {
        Add-Type -AssemblyName System.Windows.Forms
        [Windows.Forms.MessageBox]::Show($_.Exception.Message, 'Infernux Hub uninstall failed') | Out-Null
    }
    Write-Error $_ -ErrorAction Continue
    exit 1
}

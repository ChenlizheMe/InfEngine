param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$')]
    [string]$Version,
    [switch]$Publish,
    [switch]$Force,
    [switch]$UploadOnly
)

$ErrorActionPreference = 'Stop'
function Get-Sha256([string]$Path) {
    $Stream = [IO.File]::OpenRead($Path)
    try {
        $Algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            return ([BitConverter]::ToString($Algorithm.ComputeHash($Stream))).Replace('-', '').ToLowerInvariant()
        } finally {
            $Algorithm.Dispose()
        }
    } finally {
        $Stream.Dispose()
    }
}

$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ReleaseRoot = [IO.Path]::GetFullPath((Join-Path $Root "dist\release"))
$ReleaseDir = [IO.Path]::GetFullPath((Join-Path $ReleaseRoot $Version))
if (-not $ReleaseDir.StartsWith($ReleaseRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe release output path: $ReleaseDir"
}

Set-Location $Root
$ProjectText = Get-Content -LiteralPath (Join-Path $Root 'pyproject.toml') -Raw
$VersionMatch = [regex]::Match($ProjectText, '(?m)^version\s*=\s*"([^"]+)"')
if (-not $VersionMatch.Success) { throw 'Could not read project.version from pyproject.toml.' }
if ($VersionMatch.Groups[1].Value -ne $Version) {
    throw "Requested version $Version does not match pyproject.toml version $($VersionMatch.Groups[1].Value). Update project metadata first."
}
$UpdateLog = Join-Path $Root 'UpdateLog.md'
if (-not (Test-Path -LiteralPath $UpdateLog -PathType Leaf) -or (Get-Item $UpdateLog).Length -eq 0) {
    throw 'UpdateLog.md is required and must not be empty.'
}
if (-not ((Get-Content -LiteralPath $UpdateLog -Raw).Contains($Version))) {
    throw "UpdateLog.md must mention release version $Version."
}

function Test-TrustedSignature([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        return (Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop).Status -eq 'Valid'
    } catch {
        # Signing is optional. A stripped or unavailable PowerShell security
        # module must not block an otherwise valid local release build.
        return $false
    }
}

function Invoke-GhWithRetry([string]$Description, [scriptblock]$Operation) {
    for ($Attempt = 1; $Attempt -le 5; $Attempt++) {
        & $Operation
        if ($LASTEXITCODE -eq 0) { return }
        if ($Attempt -eq 5) { throw "$Description failed after $Attempt attempts." }
        $Delay = $Attempt * 3
        Write-Warning "$Description failed (attempt $Attempt/5); retrying in $Delay seconds."
        Start-Sleep -Seconds $Delay
    }
}

function Test-GitHubRelease([string]$ApiUrl, [string]$TagName) {
    for ($Attempt = 1; $Attempt -le 5; $Attempt++) {
        try {
            $null = Invoke-RestMethod -Uri $ApiUrl -Headers @{
                Accept = 'application/vnd.github+json'
                'User-Agent' = 'Infernux-Local-Release'
                'X-GitHub-Api-Version' = '2022-11-28'
            }
            return $true
        } catch {
            $StatusCode = [int]$_.Exception.Response.StatusCode
            if ($StatusCode -eq 404) { return $false }
            $Transient = $StatusCode -eq 0 -or $StatusCode -eq 429 -or $StatusCode -ge 500
            if ($Transient -and $Attempt -lt 5) {
                $Delay = $Attempt * 3
                Write-Warning "Release lookup for $TagName failed (HTTP $StatusCode, attempt $Attempt/5); retrying in $Delay seconds."
                Start-Sleep -Seconds $Delay
                continue
            }
            throw "Could not verify whether GitHub Release $TagName exists (HTTP $StatusCode). Refusing to build an unverified version."
        }
    }
    throw "Could not verify whether GitHub Release $TagName exists."
}

$Tag = "v$Version"
$ReleaseApi = "https://api.github.com/repos/ChenlizheMe/Infernux/releases/tags/$Tag"
$ReleaseExists = Test-GitHubRelease $ReleaseApi $Tag
if ($ReleaseExists -and -not $Force) {
    throw "GitHub Release $Tag already exists. Existing versions are immutable and will not be rebuilt or overwritten."
}
if ($ReleaseExists -and $Force) {
    Write-Warning "Force mode will rebuild and replace every asset attached to GitHub Release $Tag."
}

if ($UploadOnly) {
    if (-not (Test-Path -LiteralPath $ReleaseDir -PathType Container)) {
        throw "Upload-only release directory does not exist: $ReleaseDir"
    }
    Write-Host "Reusing locally built release assets from $ReleaseDir" -ForegroundColor Cyan
} else {
    if (Test-Path -LiteralPath $ReleaseDir) {
        Remove-Item -LiteralPath $ReleaseDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $ReleaseDir -Force | Out-Null

Write-Host "[1/6] Configuring the release preset..." -ForegroundColor Cyan
& cmake --preset release
if ($LASTEXITCODE -ne 0) { throw 'CMake configure failed.' }

Write-Host "[2/6] Building the native module and Release Player Runtime Pack..." -ForegroundColor Cyan
& cmake --build --preset release --target _Infernux --parallel
if ($LASTEXITCODE -ne 0) { throw 'Native engine build failed.' }
& cmake --build --preset release --target prebuild_player_runtime --parallel
if ($LASTEXITCODE -ne 0) { throw 'Release Player Runtime Pack build failed.' }

Write-Host "       Building the wheel from the verified local Release payload..." -ForegroundColor Cyan
$env:INFERNUX_SOURCE_DIR = $Root
& python -m build --wheel --no-isolation --outdir $ReleaseDir
if ($LASTEXITCODE -ne 0) { throw 'Wheel build failed.' }

Write-Host "[3/6] Building the Hub and installer through the Visual Studio/MSBuild preset..." -ForegroundColor Cyan
& cmake --build --preset packaging-installer
if ($LASTEXITCODE -ne 0) { throw 'Hub installer build failed.' }
$HubDir = Join-Path $Root 'dist\Infernux Hub'
$Installer = Join-Path $Root 'dist\installer\InfernuxHubInstaller.exe'
if (-not (Test-Path -LiteralPath $HubDir -PathType Container)) { throw "Hub output not found: $HubDir" }
if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) { throw "Installer output not found: $Installer" }
Copy-Item -LiteralPath $Installer -Destination (Join-Path $ReleaseDir "InfernuxHubInstaller-$Version.exe")

Write-Host "[4/6] Looking for a previous Hub manifest..." -ForegroundColor Cyan
$BaseManifest = $null
if (Get-Command gh -ErrorAction SilentlyContinue) {
    try {
        $PreviousTags = & gh release list --repo ChenlizheMe/Infernux --limit 20 --exclude-drafts --exclude-pre-releases --json tagName | ConvertFrom-Json
        $PreviousTag = $PreviousTags | Where-Object { $_.tagName -ne "v$Version" } | Select-Object -First 1
        if ($PreviousTag) {
            $PreviousRelease = & gh release view $PreviousTag.tagName --repo ChenlizheMe/Infernux --json assets | ConvertFrom-Json
            if ($PreviousRelease.assets.name -contains 'InfernuxHub-manifest.json') {
                $BaseDir = Join-Path $ReleaseDir '.base'
                New-Item -ItemType Directory -Path $BaseDir -Force | Out-Null
                & gh release download $PreviousTag.tagName --repo ChenlizheMe/Infernux --pattern 'InfernuxHub-manifest.json' --dir $BaseDir
                if ($LASTEXITCODE -eq 0) {
                    $Candidate = Join-Path $BaseDir 'InfernuxHub-manifest.json'
                    if (Test-Path -LiteralPath $Candidate -PathType Leaf) { $BaseManifest = $Candidate }
                }
            }
        }
    } catch {
        Write-Warning "Previous release manifest was not available; this release will contain the full package only. $($_.Exception.Message)"
    }
}

Write-Host "[5/6] Generating full and incremental Hub assets..." -ForegroundColor Cyan
$Arguments = @(
    (Join-Path $Root 'packaging\incremental_update.py'),
    '--hub-dir', $HubDir,
    '--version', $Version,
    '--output-dir', $ReleaseDir
)
if ($BaseManifest) { $Arguments += @('--base-manifest', $BaseManifest) }
& python @Arguments
if ($LASTEXITCODE -ne 0) { throw 'Hub update artifact generation failed.' }
if (Test-Path -LiteralPath (Join-Path $ReleaseDir '.base')) {
    Remove-Item -LiteralPath (Join-Path $ReleaseDir '.base') -Recurse -Force
}

$ChecksumPath = Join-Path $ReleaseDir 'SHA256SUMS.txt'
$Artifacts = Get-ChildItem -LiteralPath $ReleaseDir -File | Sort-Object Name
$ChecksumLines = foreach ($Artifact in $Artifacts) {
    $Hash = Get-Sha256 $Artifact.FullName
    "$Hash  $($Artifact.Name)"
}
[IO.File]::WriteAllLines($ChecksumPath, $ChecksumLines, [Text.UTF8Encoding]::new($false))

Write-Host "[6/6] Release assets are ready:" -ForegroundColor Green
Get-ChildItem -LiteralPath $ReleaseDir -File | Sort-Object Name | ForEach-Object {
    Write-Host ("  {0,-72} {1,10:N1} MB" -f $_.Name, ($_.Length / 1MB))
}
}

$RequiredAssets = @(
    "infernux-$Version-cp312-cp312-win_amd64.whl",
    "InfernuxHubInstaller-$Version.exe",
    "InfernuxHub-$Version-windows-x64-full.zip",
    "InfernuxHub-$Version-manifest.json",
    'InfernuxHub-manifest.json',
    'SHA256SUMS.txt'
)
foreach ($AssetName in $RequiredAssets) {
    $AssetPath = Join-Path $ReleaseDir $AssetName
    if (-not (Test-Path -LiteralPath $AssetPath -PathType Leaf) -or (Get-Item -LiteralPath $AssetPath).Length -eq 0) {
        throw "Required release asset is missing or empty: $AssetPath"
    }
}

$SignatureTargets = @((Join-Path $ReleaseDir "InfernuxHubInstaller-$Version.exe"))
if (-not $UploadOnly) {
    $SignatureTargets += (Join-Path $HubDir 'Infernux Hub.exe')
}
$UnsignedTargets = @($SignatureTargets | Where-Object { -not (Test-TrustedSignature $_) })
if ($UnsignedTargets.Count -gt 0) {
    $UnsignedList = ($UnsignedTargets -join ', ')
    $SigningHint = 'Set INFERNUX_SIGN_CERTIFICATE_THUMBPRINT to a publicly trusted Authenticode certificate before building.'
    Write-Warning "Unsigned or untrusted Windows executables remain: $UnsignedList. $SigningHint"
}

if ($Publish) {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw 'GitHub CLI (gh) is required to publish.' }
    & gh auth status
    if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not authenticated.' }
    $Files = @(Get-ChildItem -LiteralPath $ReleaseDir -File | ForEach-Object { $_.FullName })
    if (-not $ReleaseExists) {
        Invoke-GhWithRetry "Creating GitHub Release $Tag" {
            & gh release create $Tag --repo ChenlizheMe/Infernux --title "Infernux v$Version" --notes-file $UpdateLog --latest
            if ($LASTEXITCODE -ne 0) {
                & gh release view $Tag --repo ChenlizheMe/Infernux *> $null
            }
        }
    }

    Invoke-GhWithRetry "Uploading assets for $Tag" {
        & gh release upload $Tag @Files --clobber --repo ChenlizheMe/Infernux
    }

    if ($Force -and $ReleaseExists) {
        $LocalNames = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($File in $Files) { $null = $LocalNames.Add([IO.Path]::GetFileName($File)) }
        $RemoteJson = $null
        Invoke-GhWithRetry "Reading replaced assets for $Tag" {
            $script:RemoteJson = & gh release view $Tag --repo ChenlizheMe/Infernux --json assets
        }
        $RemoteRelease = $RemoteJson | ConvertFrom-Json
        foreach ($RemoteAsset in $RemoteRelease.assets) {
            if (-not $LocalNames.Contains($RemoteAsset.name)) {
                Invoke-GhWithRetry "Removing stale release asset '$($RemoteAsset.name)'" {
                    & gh release delete-asset $Tag $RemoteAsset.name --yes --repo ChenlizheMe/Infernux
                }
            }
        }
    }

    Invoke-GhWithRetry "Updating metadata for $Tag" {
        & gh release edit $Tag --repo ChenlizheMe/Infernux --title "Infernux v$Version" --notes-file $UpdateLog --latest
    }
    Write-Host "Published GitHub Release $Tag." -ForegroundColor Green
} else {
    Write-Host 'Publish was skipped. Run release_hub.bat <version> to rebuild and publish while the tag is still absent.' -ForegroundColor Yellow
}

param(
    [Parameter(Mandatory = $true)]
    [string]$OutputRoot
)

$ErrorActionPreference = 'Stop'
$SwiftShaderCommit = '694585a05946e1ed49b6bd577ca6537cbb57f025'
$Root = [IO.Path]::GetFullPath($OutputRoot)
$Work = Join-Path $Root 'work'
$Source = Join-Path $Work 'source'
$Build = Join-Path $Work 'build'
$Runtime = Join-Path $Root 'runtime'
$Manifest = Join-Path $Build 'Windows\vk_swiftshader_icd.json'
$InstalledDriver = Join-Path $Build 'Windows\vk_swiftshader.dll'
$RuntimeManifest = Join-Path $Runtime 'vk_swiftshader_icd.json'
$StagedRuntime = Join-Path $Work 'runtime'

$ResolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
$ResolvedWork = [IO.Path]::GetFullPath($Work)
if (-not $ResolvedWork.StartsWith($ResolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "SwiftShader work directory escaped its output root: $ResolvedWork"
}

function Remove-SwiftShaderWork {
    if (Test-Path -LiteralPath $ResolvedWork) {
        Remove-Item -LiteralPath $ResolvedWork -Recurse -Force
    }
}

New-Item -ItemType Directory -Path $Root -Force | Out-Null
Remove-SwiftShaderWork

try {
    New-Item -ItemType Directory -Path $Work -Force | Out-Null
    git init $Source
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the SwiftShader source repository.' }
    git -C $Source remote add origin https://github.com/google/swiftshader.git
    if ($LASTEXITCODE -ne 0) { throw 'Could not configure the SwiftShader source repository.' }
    git -C $Source fetch --depth 1 origin $SwiftShaderCommit
    if ($LASTEXITCODE -ne 0) { throw "Could not fetch SwiftShader $SwiftShaderCommit." }
    git -C $Source checkout --detach FETCH_HEAD
    if ($LASTEXITCODE -ne 0) { throw "Could not check out SwiftShader $SwiftShaderCommit." }

    cmake -S $Source -B $Build -G 'Visual Studio 17 2022' -A x64 -T host=x64 `
        -DSWIFTSHADER_BUILD_TESTS=OFF `
        -DSWIFTSHADER_BUILD_BENCHMARKS=OFF `
        -DSWIFTSHADER_WARNINGS_AS_ERRORS=OFF
    if ($LASTEXITCODE -ne 0) { throw 'SwiftShader configuration failed.' }
    cmake --build $Build --config Release --target vk_swiftshader --parallel 8
    if ($LASTEXITCODE -ne 0) { throw 'SwiftShader Vulkan ICD build failed.' }

    if (-not (Test-Path -LiteralPath $Manifest -PathType Leaf)) {
        throw "SwiftShader did not generate its Windows ICD manifest: $Manifest"
    }
    if (-not (Test-Path -LiteralPath $InstalledDriver -PathType Leaf)) {
        throw "SwiftShader did not place its Windows Vulkan ICD beside the manifest: $InstalledDriver"
    }

    $Document = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
    if ($Document.ICD.library_path -ne '.\vk_swiftshader.dll') {
        throw "SwiftShader generated an unexpected ICD library path: $($Document.ICD.library_path)"
    }

    New-Item -ItemType Directory -Path $StagedRuntime -Force | Out-Null
    Copy-Item -LiteralPath $Manifest -Destination (Join-Path $StagedRuntime 'vk_swiftshader_icd.json')
    Copy-Item -LiteralPath $InstalledDriver -Destination (Join-Path $StagedRuntime 'vk_swiftshader.dll')
    if (Test-Path -LiteralPath $Runtime) {
        Remove-Item -LiteralPath $Runtime -Recurse -Force
    }
    Move-Item -LiteralPath $StagedRuntime -Destination $Runtime
    Set-Content -LiteralPath (Join-Path $Root 'commit.txt') -Value $SwiftShaderCommit -Encoding ascii
}
finally {
    Remove-SwiftShaderWork
}
Write-Output $RuntimeManifest

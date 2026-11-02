[CmdletBinding()]
param(
    [string]$RepositoryRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($RepositoryRoot)) {
    $RepositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
}
else {
    $RepositoryRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
}

$pyprojectPath = Join-Path $RepositoryRoot "pyproject.toml"
$deploymentDir = Join-Path $RepositoryRoot "deployment\windows"
$sourceAddonPath = Join-Path $RepositoryRoot "addon.py"
$bundledAddonPath = Join-Path $RepositoryRoot "src\blender_mcp\bundled\addon.py"
$workerPath = Join-Path $RepositoryRoot "src\blender_mcp\bundled\asset_prepare_worker.py"
$configTemplatePath = Join-Path $deploymentDir "blender-mcp.local.example.psd1"

foreach ($requiredPath in @($pyprojectPath, $deploymentDir, $sourceAddonPath, $bundledAddonPath, $workerPath, $configTemplatePath)) {
    if (-not (Test-Path -LiteralPath $requiredPath)) {
        throw "Required build input not found: '$requiredPath'."
    }
}

$pyprojectText = Get-Content -LiteralPath $pyprojectPath -Raw -Encoding UTF8
$versionMatch = [regex]::Match($pyprojectText, '(?m)^version\s*=\s*"(?<version>[^"]+)"\s*$')
if (-not $versionMatch.Success) {
    throw "Could not read [project].version from '$pyprojectPath'."
}

$projectVersion = $versionMatch.Groups["version"].Value
$wheelName = "blender_mcp-$projectVersion-py3-none-any.whl"
$sourceAddonHash = (Get-FileHash -LiteralPath $sourceAddonPath -Algorithm SHA256).Hash.ToLowerInvariant()
$bundledAddonHash = (Get-FileHash -LiteralPath $bundledAddonPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($sourceAddonHash -ne $bundledAddonHash) {
    throw "addon.py and src\blender_mcp\bundled\addon.py differ. Synchronize the bundled Add-on before building."
}

$configTemplateText = Get-Content -LiteralPath $configTemplatePath -Raw -Encoding UTF8
if ($configTemplateText -notmatch 'TunnelId\s*=\s*"<TUNNEL_ID>"') {
    throw "The distribution config template must contain the <TUNNEL_ID> placeholder."
}
if ($configTemplateText -match 'OPENAI_API_KEY|ControlPlaneApiKey') {
    throw "The distribution config template must not contain API-key configuration."
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv -or -not $uv.Source) {
    throw "uv was not found on PATH. Install uv and reopen PowerShell."
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-ZipEntrySha256 {
    param(
        [Parameter(Mandatory = $true)]
        [System.IO.Compression.ZipArchive]$Archive,

        [Parameter(Mandatory = $true)]
        [string]$EntryName
    )

    $entry = $Archive.GetEntry($EntryName)
    if ($null -eq $entry) {
        throw "Wheel is missing required entry '$EntryName'."
    }

    $stream = $entry.Open()
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha256.ComputeHash($stream)
        return ([System.BitConverter]::ToString($hashBytes)).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("blender-mcp-release-" + [guid]::NewGuid().ToString("N"))
$wheelBuildDir = Join-Path $tempRoot "wheel"
$stagingRoot = Join-Path $tempRoot "package-root"
$packageDir = Join-Path $stagingRoot "blender-mcp"
$tempZipPath = Join-Path $tempRoot "blender-mcp.zip"

try {
    New-Item -ItemType Directory -Path $wheelBuildDir -Force | Out-Null
    New-Item -ItemType Directory -Path $packageDir -Force | Out-Null

    Write-Host "Building $wheelName from $RepositoryRoot ..."
    Push-Location -LiteralPath $RepositoryRoot
    try {
        & $uv.Source build --wheel --out-dir $wheelBuildDir .
        if ($LASTEXITCODE -ne 0) {
            throw "uv build failed with exit code $LASTEXITCODE."
        }
    }
    finally {
        Pop-Location
    }

    $builtWheelPath = Join-Path $wheelBuildDir $wheelName
    if (-not (Test-Path -LiteralPath $builtWheelPath -PathType Leaf)) {
        throw "Expected wheel was not produced: '$builtWheelPath'."
    }

    $wheelArchive = [System.IO.Compression.ZipFile]::OpenRead($builtWheelPath)
    try {
        $wheelAddonHash = Get-ZipEntrySha256 -Archive $wheelArchive -EntryName "blender_mcp/bundled/addon.py"
        $wheelWorkerHash = Get-ZipEntrySha256 -Archive $wheelArchive -EntryName "blender_mcp/bundled/asset_prepare_worker.py"
    }
    finally {
        $wheelArchive.Dispose()
    }

    if ($wheelAddonHash -ne $bundledAddonHash) {
        throw "The Add-on embedded in the wheel does not match src\blender_mcp\bundled\addon.py."
    }
    $sourceWorkerHash = (Get-FileHash -LiteralPath $workerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($wheelWorkerHash -ne $sourceWorkerHash) {
        throw "The worker embedded in the wheel does not match src\blender_mcp\bundled\asset_prepare_worker.py."
    }

    $distDir = Join-Path $RepositoryRoot "dist"
    if (-not (Test-Path -LiteralPath $distDir -PathType Container)) {
        New-Item -ItemType Directory -Path $distDir -Force | Out-Null
    }
    Copy-Item -LiteralPath $builtWheelPath -Destination (Join-Path $distDir $wheelName) -Force

    # deployment/windows is the release source. Remove only obsolete versioned wheel artifacts.
    Get-ChildItem -LiteralPath $deploymentDir -Filter "blender_mcp-*-py3-none-any.whl" -File |
        Where-Object { $_.Name -ne $wheelName } |
        Remove-Item -Force
    $deploymentWheelPath = Join-Path $deploymentDir $wheelName
    Copy-Item -LiteralPath $builtWheelPath -Destination $deploymentWheelPath -Force

    $manualFileName = -join @([char]0x4F7F, [char]0x7528, [char]0x624B, [char]0x518C)
    $manualFileName = $manualFileName + ".md"
    $packageFiles = @(
        $wheelName,
        "init.ps1",
        "install-addon.ps1",
        "start.ps1",
        "blender-mcp.local.example.psd1",
        $manualFileName
    )

    foreach ($fileName in $packageFiles) {
        $sourcePath = Join-Path $deploymentDir $fileName
        if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
            throw "Release package input is missing: '$sourcePath'."
        }
        Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $packageDir $fileName) -Force
    }

    # Keep the ignored local working deployment current without touching its private Tunnel ID.
    $localDeploymentDir = Join-Path $deploymentDir "blender-mcp"
    if (Test-Path -LiteralPath $localDeploymentDir -PathType Container) {
        Get-ChildItem -LiteralPath $localDeploymentDir -Filter "blender_mcp-*-py3-none-any.whl" -File |
            Where-Object { $_.Name -ne $wheelName } |
            Remove-Item -Force

        foreach ($fileName in $packageFiles) {
            Copy-Item -LiteralPath (Join-Path $deploymentDir $fileName) -Destination (Join-Path $localDeploymentDir $fileName) -Force
        }
        Write-Host "Updated local deployment assets; blender-mcp.local.psd1 was preserved."
    }

    $releaseArchive = [System.IO.Compression.ZipFile]::Open($tempZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($fileName in $packageFiles) {
            $sourcePath = Join-Path $packageDir $fileName
            $entryName = "blender-mcp/$fileName"
            $entry = $releaseArchive.CreateEntry($entryName, [System.IO.Compression.CompressionLevel]::Optimal)
            $sourceStream = [System.IO.File]::OpenRead($sourcePath)
            $entryStream = $entry.Open()
            try {
                $sourceStream.CopyTo($entryStream)
            }
            finally {
                $entryStream.Dispose()
                $sourceStream.Dispose()
            }
        }
    }
    finally {
        $releaseArchive.Dispose()
    }

    $releaseArchive = [System.IO.Compression.ZipFile]::OpenRead($tempZipPath)
    try {
        $actualEntries = @($releaseArchive.Entries | ForEach-Object { $_.FullName })
        $expectedEntries = @($packageFiles | ForEach-Object { "blender-mcp/$_" })
        $entryDifference = Compare-Object ($expectedEntries | Sort-Object) ($actualEntries | Sort-Object)
        if ($null -ne $entryDifference) {
            throw "Release ZIP contents differ from the explicit package file allowlist."
        }

        $privateConfigEntries = @($actualEntries | Where-Object { $_ -match '(^|/)blender-mcp\.local\.psd1$' })
        if ($privateConfigEntries.Count -gt 0) {
            throw "Release ZIP contains a machine-local blender-mcp.local.psd1 file."
        }

        $templateEntry = $releaseArchive.GetEntry("blender-mcp/blender-mcp.local.example.psd1")
        if ($null -eq $templateEntry) {
            throw "Release ZIP does not contain blender-mcp.local.example.psd1."
        }
        $templateStream = $templateEntry.Open()
        $templateReader = New-Object System.IO.StreamReader($templateStream, [System.Text.Encoding]::UTF8, $true)
        try {
            $archivedTemplateText = $templateReader.ReadToEnd()
        }
        finally {
            $templateReader.Dispose()
        }
        if ($archivedTemplateText -notmatch 'TunnelId\s*=\s*"<TUNNEL_ID>"') {
            throw "The archived config template does not contain the Tunnel ID placeholder."
        }
        if ($archivedTemplateText -match 'OPENAI_API_KEY|ControlPlaneApiKey') {
            throw "The archived config template contains API-key configuration."
        }
    }
    finally {
        $releaseArchive.Dispose()
    }

    $finalZipPath = Join-Path $deploymentDir "blender-mcp.zip"
    Copy-Item -LiteralPath $tempZipPath -Destination $finalZipPath -Force

    $deployedWheelHash = (Get-FileHash -LiteralPath $deploymentWheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $builtWheelHash = (Get-FileHash -LiteralPath $builtWheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $finalZipHash = (Get-FileHash -LiteralPath $finalZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($deployedWheelHash -ne $builtWheelHash) {
        throw "Deployed wheel SHA256 does not match the built wheel."
    }

    Write-Host ""
    Write-Host "WINDOWS_RELEASE_BUILD_SUCCESS"
    Write-Host "Version:       $projectVersion"
    Write-Host "Wheel:         $deploymentWheelPath"
    Write-Host "Wheel SHA256:  $deployedWheelHash"
    Write-Host "ZIP:           $finalZipPath"
    Write-Host "ZIP SHA256:    $finalZipHash"
    Write-Host "Addon SHA256:  $wheelAddonHash"
    Write-Host "Worker SHA256: $wheelWorkerHash"
}
finally {
    if (Test-Path -LiteralPath $tempRoot -PathType Container) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}

[CmdletBinding()]
param(
    [string]$WheelPath
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($WheelPath)) {
    $wheelCandidates = @(Get-ChildItem -LiteralPath $PSScriptRoot -Filter "blender_mcp-*-py3-none-any.whl" -File)
    if ($wheelCandidates.Count -ne 1) {
        throw "Expected exactly one blender_mcp-*-py3-none-any.whl beside this script; found $($wheelCandidates.Count). Pass -WheelPath explicitly or remove obsolete wheel files."
    }
    $WheelPath = $wheelCandidates[0].FullName
}

$WheelPath = [System.IO.Path]::GetFullPath($WheelPath)
if (-not (Test-Path -LiteralPath $WheelPath -PathType Leaf)) {
    throw "Blender MCP wheel not found: '$WheelPath'. Keep the wheel beside this script or pass -WheelPath."
}

$uvx = Get-Command uvx -ErrorAction SilentlyContinue
if (-not $uvx -or -not $uvx.Source) {
    throw "uvx was not found on PATH. Install uv first, then open a new PowerShell window."
}

Write-Host "Force-reinstalling the Blender MCP package and installing its Add-on from:"
Write-Host "  $WheelPath"
& $uvx.Source --reinstall-package blender-mcp --from $WheelPath blender-mcp install-addon
if ($LASTEXITCODE -ne 0) {
    throw "Blender MCP Add-on installation failed with exit code $LASTEXITCODE."
}

Write-Host "Add-on installed. In Blender, disable and re-enable 'Interface: MCP for Blender' or restart Blender, then click Start MCP Server."

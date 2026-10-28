[CmdletBinding()]
param(
    [string]$WheelPath = (Join-Path $PSScriptRoot "blender_mcp-1.9.1-py3-none-any.whl")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$WheelPath = [System.IO.Path]::GetFullPath($WheelPath)
if (-not (Test-Path -LiteralPath $WheelPath -PathType Leaf)) {
    throw "Blender MCP wheel not found: '$WheelPath'. Keep the wheel beside this script or pass -WheelPath."
}

$uvx = Get-Command uvx -ErrorAction SilentlyContinue
if (-not $uvx -or -not $uvx.Source) {
    throw "uvx was not found on PATH. Install uv first, then open a new PowerShell window."
}

Write-Host "Installing the bundled MCP for Blender add-on from:"
Write-Host "  $WheelPath"
& $uvx.Source --from $WheelPath blender-mcp install-addon
if ($LASTEXITCODE -ne 0) {
    throw "Blender MCP add-on installation failed with exit code $LASTEXITCODE."
}

Write-Host "Add-on installed. In Blender, disable and re-enable 'Interface: MCP for Blender' or restart Blender, then click Start MCP Server."

[CmdletBinding()]
param(
    [string]$ConfigPath = (Join-Path $PSScriptRoot "blender-mcp.local.psd1"),
    [string]$ProfileDir,
    [switch]$SkipAddonInstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ConfigPath = [System.IO.Path]::GetFullPath($ConfigPath)
$exampleConfigPath = Join-Path $PSScriptRoot "blender-mcp.local.example.psd1"
$wheelPath = Join-Path $PSScriptRoot "blender_mcp-1.9.1-py3-none-any.whl"
$profileName = "blender-mcp"

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    if (-not (Test-Path -LiteralPath $exampleConfigPath -PathType Leaf)) {
        throw "Configuration template not found: '$exampleConfigPath'."
    }
    Copy-Item -LiteralPath $exampleConfigPath -Destination $ConfigPath
    Write-Host "Created local configuration: $ConfigPath"
    Write-Host "Edit TunnelId in this file, then run init.ps1 again."
    throw "Tunnel ID must be configured before initialization."
}

$config = Import-PowerShellDataFile -LiteralPath $ConfigPath
if ($config -isnot [hashtable] -or -not $config.ContainsKey("TunnelId")) {
    throw "Configuration '$ConfigPath' must define TunnelId."
}
$tunnelId = [string]$config.TunnelId
if ($tunnelId -notmatch '^tunnel_(?:[a-z0-9]{32}|[a-z0-9]{4}_[a-z0-9]{32})$') {
    throw "TunnelId in '$ConfigPath' is invalid. Use the tunnel ID copied from the OpenAI Tunnels settings."
}

if (-not (Test-Path -LiteralPath $wheelPath -PathType Leaf)) {
    throw "Packaged wheel not found: '$wheelPath'. Copy blender_mcp-1.9.1-py3-none-any.whl beside the deployment scripts."
}
$uvx = Get-Command uvx -ErrorAction SilentlyContinue
if (-not $uvx -or -not $uvx.Source) {
    throw "uvx was not found on PATH. Install uv first, then open a new PowerShell window."
}
$tunnelClient = Get-Command tunnel-client -ErrorAction SilentlyContinue
if (-not $tunnelClient -or -not $tunnelClient.Source) {
    throw "tunnel-client was not found on PATH. Install tunnel-client and reopen PowerShell."
}

if (-not $SkipAddonInstall) {
    & (Join-Path $PSScriptRoot "install-addon.ps1") -WheelPath $wheelPath
}

# start.ps1 changes its working directory to this folder before launching tunnel-client,
# so the relative wheel path below resolves even when the deployment folder is moved.
$mcpCommand = 'uvx --from blender_mcp-1.9.1-py3-none-any.whl blender-mcp'
Write-Host "Initializing tunnel-client profile '$profileName' with sample_mcp_stdio_local..."
Write-Host "Tunnel ID: configured (value hidden)"
Write-Host "MCP command: $mcpCommand"
$initArgs = @(
    "init",
    "--sample", "sample_mcp_stdio_local",
    "--profile", $profileName,
    "--tunnel-id", $tunnelId,
    "--mcp-command", $mcpCommand,
    "--control-plane-api-key-ref", "env:OPENAI_API_KEY",
    "--force"
)
if (-not [string]::IsNullOrWhiteSpace($ProfileDir)) {
    $initArgs += @("--profile-dir", [System.IO.Path]::GetFullPath($ProfileDir))
}
& $tunnelClient.Source @initArgs
if ($LASTEXITCODE -ne 0) {
    throw "tunnel-client init failed with exit code $LASTEXITCODE."
}

Write-Host "Profile initialized. Set OPENAI_API_KEY in the environment, then run .\start.ps1."

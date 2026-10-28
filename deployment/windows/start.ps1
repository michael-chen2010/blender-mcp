[CmdletBinding()]
param(
    [switch]$Doctor
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$profileName = "blender-mcp"
$tunnelClient = Get-Command tunnel-client -ErrorAction SilentlyContinue

if (-not $tunnelClient -or -not $tunnelClient.Source) {
    throw "tunnel-client was not found on PATH. Install tunnel-client and reopen PowerShell."
}

if ([string]::IsNullOrWhiteSpace($env:OPENAI_API_KEY)) {
    throw "OPENAI_API_KEY is not set for this PowerShell process. Set it before starting tunnel-client; the key is not stored in the profile."
}

try {
    $Host.UI.RawUI.WindowTitle = "blender-mcp"
} catch {
    # Hosts without a console title can still run tunnel-client.
}

# The MCP command in the profile intentionally uses the wheel filename, not an absolute path.
# Keep tunnel-client's child-process working directory at this deployment folder.
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($Doctor) {
        & $tunnelClient.Source doctor --profile $profileName
        if ($LASTEXITCODE -ne 0) {
            throw "tunnel-client doctor failed with exit code $LASTEXITCODE."
        }
        return
    }

    Write-Host "Starting tunnel-client profile '$profileName'..."
    Write-Host "Control-plane API key: loaded from OPENAI_API_KEY (value hidden)"
    & $tunnelClient.Source run --profile $profileName
    if ($LASTEXITCODE -ne 0) {
        throw "tunnel-client run exited with code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

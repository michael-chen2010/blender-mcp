from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_DIR = REPO_ROOT / "deployment" / "windows"
WHEEL_NAME = "blender_mcp-1.9.1-py3-none-any.whl"


def test_deployment_has_addon_install_init_start_and_config_template() -> None:
    for name in (
        "install-addon.ps1",
        "init.ps1",
        "start.ps1",
        "blender-mcp.local.example.psd1",
    ):
        assert (DEPLOYMENT_DIR / name).is_file(), f"Missing deployment file: {name}"


def test_deployment_wheel_is_the_built_local_wheel() -> None:
    wheel = DEPLOYMENT_DIR / WHEEL_NAME
    assert wheel.is_file(), f"Copy the built wheel to deployment/windows: {wheel}"
    assert wheel.stat().st_size > 100_000


def test_addon_installer_uses_the_deployed_wheel_without_source_checkout() -> None:
    script = (DEPLOYMENT_DIR / "install-addon.ps1").read_text(encoding="utf-8")
    assert WHEEL_NAME in script
    assert "uvx" in script
    assert "install-addon" in script
    assert "E:\\Work\\blender-mcp" not in script
    assert "--from" in script


def test_init_uses_tunnel_client_on_path_and_stdio_wheel_command() -> None:
    script = (DEPLOYMENT_DIR / "init.ps1").read_text(encoding="utf-8")
    assert "tunnel-client init" in script
    assert "--sample" in script and "sample_mcp_stdio_local" in script
    assert "--profile" in script and "blender-mcp" in script
    assert "--tunnel-id" in script
    assert "--mcp-command" in script
    assert "--force" in script
    assert "env:OPENAI_API_KEY" in script
    assert "uvx --from blender_mcp-1.9.1-py3-none-any.whl blender-mcp" in script
    assert "client\\tunnel-client" not in script
    assert "AssetPlatformRoot" not in script


def test_start_runs_the_configured_tunnel_profile_from_deployment_directory() -> None:
    script = (DEPLOYMENT_DIR / "start.ps1").read_text(encoding="utf-8")
    assert "tunnel-client run" in script
    assert "blender-mcp" in script
    assert "OPENAI_API_KEY" in script
    assert "Push-Location -LiteralPath $PSScriptRoot" in script
    assert "client\\tunnel-client" not in script
    assert "AssetPlatformRoot" not in script


def test_config_template_has_tunnel_id_but_no_api_key_secret() -> None:
    config = (DEPLOYMENT_DIR / "blender-mcp.local.example.psd1").read_text(encoding="utf-8")
    assert 'TunnelId = "<TUNNEL_ID>"' in config
    assert "OPENAI_API_KEY" not in config
    assert "ControlPlaneApiKey" not in config

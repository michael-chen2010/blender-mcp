from __future__ import annotations

import re
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_DIR = REPO_ROOT / "deployment" / "windows"
PROJECT_VERSION = re.search(
    r'(?m)^version\s*=\s*"([^"]+)"\s*$',
    (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
).group(1)
WHEEL_NAME = f"blender_mcp-{PROJECT_VERSION}-py3-none-any.whl"


def test_deployment_has_addon_install_init_start_and_config_template() -> None:
    for name in (
        "install-addon.ps1",
        "init.ps1",
        "start.ps1",
        "build-release.ps1",
        "blender-mcp.local.example.psd1",
    ):
        assert (DEPLOYMENT_DIR / name).is_file(), f"Missing deployment file: {name}"


def test_deployment_wheel_is_the_built_local_wheel() -> None:
    wheel = DEPLOYMENT_DIR / WHEEL_NAME
    assert wheel.is_file(), f"Copy the built wheel to deployment/windows: {wheel}"
    assert wheel.stat().st_size > 100_000
    with zipfile.ZipFile(wheel) as archive:
        assert "blender_mcp/bundled/addon.py" in archive.namelist()
        assert "blender_mcp/bundled/asset_prepare_worker.py" in archive.namelist()


def test_addon_installer_uses_and_forces_reinstall_of_the_deployed_wheel() -> None:
    script = (DEPLOYMENT_DIR / "install-addon.ps1").read_text(encoding="utf-8")
    assert "blender_mcp-*-py3-none-any.whl" in script
    assert "--reinstall-package" in script
    assert "uvx" in script
    assert "install-addon" in script
    assert r"E:\Work\blender-mcp" not in script
    assert "--from" in script


def test_init_uses_tunnel_client_on_path_and_discovers_the_versioned_wheel() -> None:
    script = (DEPLOYMENT_DIR / "init.ps1").read_text(encoding="utf-8")
    assert "tunnel-client init" in script
    assert "--sample" in script and "sample_mcp_stdio_local" in script
    assert "--profile" in script and "blender-mcp" in script
    assert "--tunnel-id" in script
    assert "--mcp-command" in script
    assert "--force" in script
    assert "env:OPENAI_API_KEY" in script
    assert "blender_mcp-*-py3-none-any.whl" in script
    assert "uvx --reinstall-package blender-mcp --from $wheelName blender-mcp" in script
    assert r"client\tunnel-client" not in script
    assert "AssetPlatformRoot" not in script


def test_start_runs_the_configured_tunnel_profile_from_deployment_directory() -> None:
    script = (DEPLOYMENT_DIR / "start.ps1").read_text(encoding="utf-8")
    assert "tunnel-client run" in script
    assert "blender-mcp" in script
    assert "OPENAI_API_KEY" in script
    assert "Push-Location -LiteralPath $PSScriptRoot" in script
    assert r"client\tunnel-client" not in script
    assert "AssetPlatformRoot" not in script


def test_config_template_has_tunnel_id_but_no_api_key_secret() -> None:
    config = (DEPLOYMENT_DIR / "blender-mcp.local.example.psd1").read_text(encoding="utf-8")
    assert 'TunnelId = "<TUNNEL_ID>"' in config
    assert "OPENAI_API_KEY" not in config
    assert "ControlPlaneApiKey" not in config


def test_release_builder_builds_and_packages_a_verified_wheel() -> None:
    script = (DEPLOYMENT_DIR / "build-release.ps1").read_text(encoding="utf-8")
    assert "uv build" in script
    assert "--wheel" in script
    assert "pyproject.toml" in script
    assert "SHA256" in script
    assert "CreateEntry" in script
    assert "ZipArchiveMode]::Create" in script
    assert "blender-mcp.local.example.psd1" in script
    assert "blender-mcp.local.psd1" in script


def test_release_archive_contains_config_template_and_excludes_local_config() -> None:
    archive_path = DEPLOYMENT_DIR / "blender-mcp.zip"
    assert archive_path.is_file()
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert "blender-mcp/blender-mcp.local.example.psd1" in names
    assert "blender-mcp/blender-mcp.local.psd1" not in names
    assert f"blender-mcp/{WHEEL_NAME}" in names
    assert "blender-mcp/init.ps1" in names
    assert "blender-mcp/install-addon.ps1" in names
    assert "blender-mcp/start.ps1" in names
    assert "blender-mcp/使用手册.md" in names

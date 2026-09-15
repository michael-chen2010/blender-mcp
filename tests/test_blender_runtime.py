"""Tests for the local Blender runtime used by asset preparation workers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blender_mcp.blender_runtime import (
    ASSET_CONCURRENCY_ENV,
    BlenderRuntime,
    configured_asset_concurrency,
    recommended_asset_concurrency,
    resolve_blender_runtime,
)


def _touch(path: Path) -> Path:
    path.write_bytes(b"fake blender executable")
    return path


def test_runtime_resolver_prefers_explicit_env(tmp_path: Path):
    env_blender = _touch(tmp_path / "env-blender")
    addon_blender = _touch(tmp_path / "addon-blender")
    path_blender = _touch(tmp_path / "path-blender")

    runtime = resolve_blender_runtime(
        env={"BLENDERMCP_BLENDER_EXECUTABLE": str(env_blender)},
        addon_binary_path=str(addon_blender),
        blender_host="localhost",
        which=lambda _name: str(path_blender),
    )

    assert runtime == BlenderRuntime(str(env_blender), "ENV")


def test_runtime_resolver_uses_local_addon_binary_before_path(tmp_path: Path):
    addon_blender = _touch(tmp_path / "addon-blender")
    path_blender = _touch(tmp_path / "path-blender")

    runtime = resolve_blender_runtime(
        env={},
        addon_binary_path=str(addon_blender),
        blender_host="127.0.0.1",
        which=lambda _name: str(path_blender),
    )

    assert runtime == BlenderRuntime(str(addon_blender), "LOCAL_ADDON")


def test_runtime_resolver_never_uses_remote_addon_path(tmp_path: Path):
    remote_reported_blender = _touch(tmp_path / "remote-machine-path")
    path_blender = _touch(tmp_path / "path-blender")

    runtime = resolve_blender_runtime(
        env={},
        addon_binary_path=str(remote_reported_blender),
        blender_host="192.0.2.10",
        which=lambda _name: str(path_blender),
    )

    assert runtime == BlenderRuntime(str(path_blender), "PATH")


def test_runtime_resolver_falls_back_to_path(tmp_path: Path):
    path_blender = _touch(tmp_path / "path-blender")

    runtime = resolve_blender_runtime(
        env={},
        addon_binary_path=None,
        blender_host=None,
        which=lambda name: str(path_blender) if name == "blender" else None,
    )

    assert runtime == BlenderRuntime(str(path_blender), "PATH")


def test_runtime_resolver_reports_unavailable_when_no_candidate_exists():
    with pytest.raises(FileNotFoundError, match="Blender executable"):
        resolve_blender_runtime(
            env={},
            addon_binary_path=None,
            blender_host=None,
            which=lambda _name: None,
        )


@pytest.mark.parametrize(
    ("cpu_count", "expected"),
    [(1, 1), (4, 2), (8, 4), (16, 8), (24, 12), (64, 12)],
)
def test_recommended_asset_concurrency_is_cpu_derived_and_capped(cpu_count: int, expected: int):
    assert recommended_asset_concurrency(cpu_count) == expected


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("0", 1), ("1", 1), ("4", 4), ("12", 12), ("99", 12)],
)
def test_configured_asset_concurrency_clamps_env_override(configured: str, expected: int):
    assert configured_asset_concurrency(8, {ASSET_CONCURRENCY_ENV: configured}) == expected


def test_configured_asset_concurrency_defaults_to_recommendation():
    assert configured_asset_concurrency(8, {}) == 4


def test_pipeline_status_is_local_only_and_does_not_connect_to_gui(monkeypatch):
    pytest.importorskip("mcp")
    from blender_mcp import server

    def must_not_connect():
        raise AssertionError("pipeline status must not touch the Blender GUI command queue")

    monkeypatch.setattr(server, "get_blender_connection", must_not_connect)
    monkeypatch.setattr(server.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(
        server,
        "resolve_blender_runtime",
        lambda **_kwargs: BlenderRuntime("C:/secret/blender.exe", "ENV"),
    )
    monkeypatch.delenv(ASSET_CONCURRENCY_ENV, raising=False)

    payload = json.loads(server.get_asset_pipeline_status())

    assert payload == {
        "available": True,
        "source": "ENV",
        "blenderVersion": None,
        "supportedProfiles": ["PREVIEW", "METADATA", "PUBLISH"],
        "cpuCount": 8,
        "recommendedConcurrency": 4,
        "configuredMaxConcurrency": 4,
        "hardCap": 12,
    }
    assert "secret" not in json.dumps(payload)

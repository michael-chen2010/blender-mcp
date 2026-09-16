"""Telemetry must not break MCP operation when private config.py is absent."""

from __future__ import annotations

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from blender_mcp import telemetry


@pytest.fixture
def isolated_telemetry(monkeypatch):
    monkeypatch.setattr(telemetry, "_telemetry_collector", None)
    monkeypatch.setattr(telemetry.TelemetryCollector, "_get_or_create_uuid", lambda self: "test-uuid")
    monkeypatch.setattr(telemetry.TelemetryCollector, "_worker_loop", lambda self: None)
    monkeypatch.delitem(sys.modules, "blender_mcp.config", raising=False)
    for name in ("DISABLE_TELEMETRY", "BLENDER_MCP_DISABLE_TELEMETRY", "MCP_DISABLE_TELEMETRY"):
        monkeypatch.delenv(name, raising=False)


def test_missing_private_config_disables_telemetry_without_breaking_tools(isolated_telemetry):
    collector = telemetry.get_telemetry()

    assert collector.config.enabled is False
    assert telemetry.is_telemetry_enabled() is False
    telemetry.record_tool_usage("get_scene_info", success=True, duration_ms=1.0)
    assert collector._queue.empty()


def test_existing_private_config_is_still_used(monkeypatch, isolated_telemetry):
    private_config = ModuleType("blender_mcp.config")
    expected = SimpleNamespace(enabled=True)
    private_config.telemetry_config = expected
    monkeypatch.setitem(sys.modules, "blender_mcp.config", private_config)

    assert telemetry.get_telemetry().config is expected


def test_addon_status_works_without_private_config(monkeypatch, isolated_telemetry):
    pytest.importorskip("mcp")
    from blender_mcp import server

    class FakeBlender:
        def send_command(self, command):
            if command == "get_addon_info":
                return {
                    "protocol_version": 6,
                    "addon_version": [1, 6],
                    "capabilities": ["create_blend_snapshot"],
                    "blender_version": "5.2.1",
                }
            assert command == "get_telemetry_consent"
            return {"consent": False}

    monkeypatch.setattr(server, "get_blender_connection", lambda: FakeBlender())
    monkeypatch.setattr(server, "_addon_handshake", None)
    monkeypatch.setattr(server, "_addon_handshake_checked", False)

    status = json.loads(asyncio.run(server.get_addon_status(ctx=None)))
    assert status["up_to_date"] is True
    assert status["protocol_version"] == 6
    assert status["telemetry_consent"] is False
    assert telemetry.get_telemetry().config.enabled is False


def test_applying_consent_invalidates_the_actual_collector(monkeypatch):
    from blender_mcp import consent_prompt, server

    class FakeBlender:
        def send_command(self, command, params):
            assert command == "set_telemetry_consent"
            assert params == {"consent": True}
            return {"consent": True}

    invalidated = []
    fake_collector = SimpleNamespace(invalidate_consent_cache=lambda: invalidated.append(True))
    monkeypatch.setattr(server, "get_blender_connection", lambda: FakeBlender())
    monkeypatch.setattr(telemetry, "get_telemetry", lambda: fake_collector)

    assert consent_prompt._apply_consent(True) is True
    assert invalidated == [True]

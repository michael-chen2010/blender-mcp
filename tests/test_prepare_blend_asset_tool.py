import asyncio
import base64
import hashlib
import json
from pathlib import Path

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from blender_mcp import server
from blender_mcp.asset_pipeline import AssetPrepareError


PNG_BYTES = b"\x89PNG\r\n\x1a\nprepared-main-preview"


class _FakeArtifactStore:
    def __init__(self, preview_path: Path):
        self.preview_path = preview_path

    def resolve(self, artifact_id: str) -> Path:
        if artifact_id != "preview-opaque":
            raise AssertionError(f"unexpected artifact id: {artifact_id}")
        return self.preview_path


def _ready_result(tmp_path: Path, *, source_kind: str = "BLEND_FILE") -> tuple[dict, Path]:
    preview_path = tmp_path / "main-preview.png"
    preview_path.write_bytes(PNG_BYTES)
    digest = hashlib.sha256(PNG_BYTES).hexdigest()
    return (
        {
            "status": "READY",
            "prepareId": "prepare-123",
            "profile": "METADATA",
            "observation": {
                "schemaVersion": 1,
                "prepareId": "prepare-123",
                "observationScope": "SOURCE_ASSET",
                "source": {
                    "kind": source_kind,
                    "displayName": "asset.blend",
                    "blenderVersion": "5.2.0",
                },
                "structure": {"objectCount": 2},
            },
            "previewPath": str(preview_path),
            "artifacts": [
                {
                    "artifact_id": "preview-opaque",
                    "kind": "PREVIEW",
                    "size": len(PNG_BYTES),
                    "sha256": digest,
                    "content_type": "image/png",
                    "expires_at": "2026-09-15T12:00:00.000Z",
                }
            ],
            "warnings": [],
            "timings": {"openMs": 1.0, "inspectMs": 2.0, "previewMs": 3.0, "totalMs": 6.0},
        },
        preview_path,
    )


def _call_tool(arguments: dict) -> CallToolResult:
    result = asyncio.run(server.mcp.call_tool("prepare_blend_asset", arguments))
    assert isinstance(result, CallToolResult)
    return result


def _error_code(result: CallToolResult) -> str:
    assert result.isError is True
    assert result.structuredContent is not None
    return result.structuredContent["error"]["code"]


def test_blend_file_bypasses_gui_socket(monkeypatch, tmp_path: Path):
    source = tmp_path / "asset.blend"
    source.write_bytes(b"BLENDER-source")
    ready, preview_path = _ready_result(tmp_path)
    calls = []

    def forbidden_connection():
        raise AssertionError("BLEND_FILE must not touch the GUI Blender socket")

    def fake_prepare(source_path, **kwargs):
        calls.append((Path(source_path), kwargs))
        return ready

    monkeypatch.setattr(server, "get_blender_connection", forbidden_connection)
    monkeypatch.setattr(server, "prepare_blend_file", fake_prepare, raising=False)
    monkeypatch.setattr(
        server,
        "get_prepared_artifact_store",
        lambda: _FakeArtifactStore(preview_path),
        raising=False,
    )

    result = _call_tool(
        {"source": {"kind": "BLEND_FILE", "path": str(source)}, "profile": "METADATA"}
    )

    assert result.isError is False
    assert len(calls) == 1
    assert calls[0][0] == source
    assert calls[0][1]["profile"] == "METADATA"
    assert calls[0][1]["source_kind"] == "BLEND_FILE"


def test_current_selection_performs_exactly_one_asset_closure_snapshot(monkeypatch, tmp_path: Path):
    ready, preview_path = _ready_result(tmp_path, source_kind="CURRENT_SELECTION")
    snapshot_calls = []
    prepare_calls = []

    def fake_snapshot(filepath: str, *, closure_mode: str):
        snapshot_calls.append((Path(filepath), closure_mode))
        Path(filepath).write_bytes(b"BLENDER-selection-snapshot")
        return {
            "filepath": filepath,
            "rootObjectNames": ["Root"],
            "includedObjectNames": ["Root", "Child"],
            "blenderVersion": "5.2.0",
            "sourceFingerprint": "snapshot-fingerprint",
        }

    def fake_prepare(source_path, **kwargs):
        prepare_calls.append((Path(source_path), kwargs))
        assert Path(source_path).is_file()
        return ready

    monkeypatch.setattr(server, "create_blend_snapshot", fake_snapshot)
    monkeypatch.setattr(server, "prepare_blend_file", fake_prepare, raising=False)
    monkeypatch.setattr(
        server,
        "get_prepared_artifact_store",
        lambda: _FakeArtifactStore(preview_path),
        raising=False,
    )

    result = _call_tool({"source": {"kind": "CURRENT_SELECTION"}, "profile": "METADATA"})

    assert result.isError is False
    assert len(snapshot_calls) == 1
    assert snapshot_calls[0][1] == "ASSET_CLOSURE"
    assert len(prepare_calls) == 1
    assert prepare_calls[0][0] == snapshot_calls[0][0]
    assert prepare_calls[0][1]["source_kind"] == "CURRENT_SELECTION"


def test_mcp_result_contains_structured_data_text_summary_and_main_image(monkeypatch, tmp_path: Path):
    source = tmp_path / "asset.blend"
    source.write_bytes(b"BLENDER-source")
    ready, preview_path = _ready_result(tmp_path)
    store = _FakeArtifactStore(preview_path)

    monkeypatch.setattr(server, "prepare_blend_file", lambda *_args, **_kwargs: ready, raising=False)
    monkeypatch.setattr(server, "get_prepared_artifact_store", lambda: store, raising=False)

    result = _call_tool(
        {"source": {"kind": "BLEND_FILE", "path": str(source)}, "profile": "METADATA"}
    )

    assert result.isError is False
    assert result.structuredContent is not None
    structured = result.structuredContent
    assert structured["prepareId"] == "prepare-123"
    assert structured["observation"] == ready["observation"]
    assert structured["timings"] == ready["timings"]
    assert structured["warnings"] == []
    assert structured["artifacts"] == [
        {
            "artifactId": "preview-opaque",
            "kind": "PREVIEW",
            "fileName": "main-preview.png",
            "size": len(PNG_BYTES),
            "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
            "mimeType": "image/png",
            "expiresAt": "2026-09-15T12:00:00.000Z",
        }
    ]
    serialized = json.dumps(structured, ensure_ascii=False)
    assert str(preview_path) not in serialized
    assert "previewPath" not in serialized
    assert base64.b64encode(PNG_BYTES).decode("ascii") not in serialized

    assert len(result.content) == 2
    assert isinstance(result.content[0], TextContent)
    assert "prepare-123" in result.content[0].text
    assert "METADATA" in result.content[0].text
    assert len(result.content[0].text) < 500
    assert isinstance(result.content[1], ImageContent)
    assert result.content[1].mimeType == "image/png"
    assert base64.b64decode(result.content[1].data) == PNG_BYTES


@pytest.mark.parametrize(
    ("arguments", "expected_code"),
    [
        ({"source": {"kind": "UNKNOWN"}, "profile": "METADATA"}, "PREPARE_INVALID_SOURCE"),
        ({"source": {"kind": "BLEND_FILE"}, "profile": "METADATA"}, "PREPARE_INVALID_SOURCE"),
        ({"source": {"kind": "CURRENT_SELECTION", "path": "x.blend"}, "profile": "METADATA"}, "PREPARE_INVALID_SOURCE"),
        ({"source": {"kind": "CURRENT_SELECTION"}, "profile": "INVALID"}, "PREPARE_INVALID_PROFILE"),
        (
            {
                "source": {"kind": "CURRENT_SELECTION"},
                "profile": "METADATA",
                "overrides": {"preview": {"width": 0}},
            },
            "PREPARE_INVALID_OVERRIDES",
        ),
        (
            {
                "source": {"kind": "CURRENT_SELECTION"},
                "profile": "METADATA",
                "overrides": {"preview": {"width": 256, "height": 256, "views": ["MAIN"]}},
            },
            "PREPARE_OVERRIDE_UNSUPPORTED",
        ),
    ],
)
def test_invalid_inputs_return_stable_errors(monkeypatch, arguments: dict, expected_code: str):
    def must_not_prepare(*_args, **_kwargs):
        raise AssertionError("invalid input must be rejected before worker preparation")

    monkeypatch.setattr(server, "prepare_blend_file", must_not_prepare, raising=False)

    result = _call_tool(arguments)

    assert _error_code(result) == expected_code
    assert isinstance(result.content[0], TextContent)
    assert expected_code in result.content[0].text


def test_missing_blend_file_returns_stable_source_error(monkeypatch, tmp_path: Path):
    missing = tmp_path / "missing.blend"

    def must_not_prepare(*_args, **_kwargs):
        raise AssertionError("missing source must be rejected before worker preparation")

    monkeypatch.setattr(server, "prepare_blend_file", must_not_prepare, raising=False)

    result = _call_tool(
        {"source": {"kind": "BLEND_FILE", "path": str(missing)}, "profile": "METADATA"}
    )

    assert _error_code(result) == "BLEND_SOURCE_NOT_FOUND"


def test_prepare_timeout_preserves_stable_worker_error_code(monkeypatch, tmp_path: Path):
    source = tmp_path / "asset.blend"
    source.write_bytes(b"BLENDER-source")

    def timed_out(*_args, **_kwargs):
        raise AssetPrepareError("BLENDER_PREPARE_TIMEOUT: background Blender exceeded 180.0s")

    monkeypatch.setattr(server, "prepare_blend_file", timed_out, raising=False)

    result = _call_tool(
        {"source": {"kind": "BLEND_FILE", "path": str(source)}, "profile": "METADATA"}
    )

    assert _error_code(result) == "BLENDER_PREPARE_TIMEOUT"
    assert "180.0s" in result.content[0].text


def test_prepare_cancellation_is_not_converted_into_a_normal_error(monkeypatch, tmp_path: Path):
    source = tmp_path / "asset.blend"
    source.write_bytes(b"BLENDER-source")

    async def cancelled_to_thread(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(server.asyncio, "to_thread", cancelled_to_thread)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            server.prepare_blend_asset(
                source={"kind": "BLEND_FILE", "path": str(source)},
                profile="METADATA",
            )
        )


def test_single_prepare_uses_server_global_asset_worker_slot(monkeypatch, tmp_path: Path):
    from contextlib import asynccontextmanager

    source = tmp_path / "asset.blend"
    source.write_bytes(b"BLENDER-source")
    ready, preview_path = _ready_result(tmp_path)
    events = []

    @asynccontextmanager
    async def fake_slot():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    def fake_prepare(*_args, **_kwargs):
        events.append("prepare")
        return ready

    monkeypatch.setattr(server, "asset_worker_slot", fake_slot, raising=False)
    monkeypatch.setattr(server, "prepare_blend_file", fake_prepare)
    monkeypatch.setattr(
        server,
        "get_prepared_artifact_store",
        lambda: _FakeArtifactStore(preview_path),
    )

    result = _call_tool(
        {"source": {"kind": "BLEND_FILE", "path": str(source)}, "profile": "METADATA"}
    )

    assert result.isError is False
    assert events == ["enter", "prepare", "exit"]


def test_prepare_uses_local_addon_runtime_resolution(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    source = tmp_path / "asset.blend"
    source.write_bytes(b"BLENDER-source")
    ready, preview_path = _ready_result(tmp_path)
    runtime = object()
    resolved = []
    prepare_runtimes = []

    monkeypatch.setattr(
        server,
        "_addon_handshake",
        SimpleNamespace(blender_binary_path="C:/Program Files/Blender/blender.exe"),
    )
    monkeypatch.setenv("BLENDER_HOST", "127.0.0.1")

    def fake_resolve_blender_runtime(**kwargs):
        resolved.append(kwargs)
        return runtime

    def fake_prepare(*_args, **kwargs):
        prepare_runtimes.append(kwargs.get("runtime"))
        return ready

    monkeypatch.setattr(server, "resolve_blender_runtime", fake_resolve_blender_runtime)
    monkeypatch.setattr(server, "prepare_blend_file", fake_prepare)
    monkeypatch.setattr(
        server,
        "get_prepared_artifact_store",
        lambda: _FakeArtifactStore(preview_path),
    )

    result = _call_tool(
        {"source": {"kind": "BLEND_FILE", "path": str(source)}, "profile": "METADATA"}
    )

    assert result.isError is False
    assert resolved == [
        {
            "addon_binary_path": "C:/Program Files/Blender/blender.exe",
            "blender_host": "127.0.0.1",
        }
    ]
    assert prepare_runtimes == [runtime]


def test_supplemental_renderer_uses_local_addon_runtime_resolution(monkeypatch, tmp_path: Path):
    from types import SimpleNamespace

    source = tmp_path / "retained.blend"
    source.write_bytes(b"BLENDER-retained")
    output = tmp_path / "front.png"
    runtime = object()
    resolved = []
    render_runtimes = []

    monkeypatch.setattr(
        server,
        "_addon_handshake",
        SimpleNamespace(blender_binary_path="C:/Program Files/Blender/blender.exe"),
    )
    monkeypatch.setenv("BLENDER_HOST", "localhost")

    def fake_resolve_blender_runtime(**kwargs):
        resolved.append(kwargs)
        return runtime

    async def fake_render_supplemental_view(**kwargs):
        render_runtimes.append(kwargs.get("runtime"))
        output.write_bytes(b"\x89PNG\r\n\x1a\nfront")
        return {"totalMs": 1.0}

    monkeypatch.setattr(server, "resolve_blender_runtime", fake_resolve_blender_runtime)
    monkeypatch.setattr(server, "render_supplemental_view", fake_render_supplemental_view)

    timings = asyncio.run(
        server._render_prepared_supplemental_view(
            source_path=source,
            output_path=output,
            view="FRONT",
            prepare_id="prepare-runtime",
        )
    )

    assert timings == {"totalMs": 1.0}
    assert resolved == [
        {
            "addon_binary_path": "C:/Program Files/Blender/blender.exe",
            "blender_host": "localhost",
        }
    ]
    assert render_runtimes == [runtime]

"""Supplemental prepared observation cache and immutable view rendering tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from blender_mcp.prepared_artifacts import PreparedArtifactStore
from blender_mcp.prepared_observation import (
    PreparedObservationError,
    PreparedObservationService,
)


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _cache_payload(prepare_id: str, *, item_key: str | None = "item-a") -> dict:
    payload = {
        "prepareId": prepare_id,
        "sections": {
            "STRUCTURE": {
                "objectCount": 4,
                "collectionCount": 2,
                "objects": {
                    "totalCount": 4,
                    "truncated": False,
                    "items": [
                        {"name": "A", "type": "MESH"},
                        {"name": "B", "type": "MESH"},
                        {"name": "C", "type": "EMPTY"},
                        {"name": "D", "type": "ARMATURE"},
                    ],
                },
            },
            "GEOMETRY": {
                "meshCount": 2,
                "triangleCount": 48,
                "dimensionsMeters": {"x": 1.0, "y": 2.0, "z": 3.0},
            },
            "MATERIALS": {
                "materialCount": 3,
                "materialWorkflow": "PBR",
                "items": {
                    "totalCount": 3,
                    "truncated": False,
                    "items": [
                        {"name": "MatA", "usesNodes": True},
                        {"name": "MatB", "usesNodes": True},
                        {"name": "MatC", "usesNodes": False},
                    ],
                },
            },
            "DEFORMATION": {
                "rigged": True,
                "animated": False,
                "armatureCount": 1,
                "actionCount": 0,
                "shapeKeyCount": 2,
            },
        },
    }
    if item_key is not None:
        payload["itemKey"] = item_key
    return payload


def _registered_prepare(
    tmp_path: Path,
    *,
    prepare_id: str = "prepare-a",
    item_key: str | None = "item-a",
    clock: _Clock | None = None,
) -> tuple[PreparedArtifactStore, Path, Path, Path]:
    workspace = tmp_path / prepare_id
    workspace.mkdir(parents=True)
    retained = workspace / "source.blend"
    retained_bytes = f"BLENDER-{prepare_id}-immutable".encode()
    retained.write_bytes(retained_bytes)
    cache = workspace / "observation-cache.json"
    cache.write_text(json.dumps(_cache_payload(prepare_id, item_key=item_key)), encoding="utf-8")
    main = workspace / "main.png"
    main.write_bytes(b"\x89PNG\r\n\x1a\nmain")

    store = PreparedArtifactStore(ttl_seconds=5, clock=clock or _Clock())
    store.register_workspace(
        prepare_id,
        workspace,
        retained_source_path=retained,
        retained_source_kind="SOURCE_SNAPSHOT",
        observation_cache_path=cache,
    )
    store.register_artifact(prepare_id, "PREVIEW", main, content_type="image/png")
    return store, workspace, retained, main


def test_inspect_pages_cached_structure_with_stable_identity_and_never_renders(tmp_path: Path):
    store, _workspace, retained, _main = _registered_prepare(tmp_path)
    render_calls = []

    async def forbidden_renderer(**_kwargs):
        render_calls.append(True)
        raise AssertionError("inspect must not launch Blender")

    service = PreparedObservationService(store, renderer=forbidden_renderer)

    first = service.inspect("prepare-a", "STRUCTURE", limit=2)
    second = service.inspect("prepare-a", "STRUCTURE", cursor=first["nextCursor"], limit=2)

    expected_identity = {
        "prepareId": "prepare-a",
        "kind": "SOURCE_SNAPSHOT",
        "size": retained.stat().st_size,
        "sha256": _sha256(retained.read_bytes()),
    }
    assert first["prepareId"] == second["prepareId"] == "prepare-a"
    assert first["itemKey"] == second["itemKey"] == "item-a"
    assert first["section"] == second["section"] == "STRUCTURE"
    assert first["boundedDetails"] == {
        "summary": {"objectCount": 4, "collectionCount": 2},
        "totalCount": 4,
        "items": [{"name": "A", "type": "MESH"}, {"name": "B", "type": "MESH"}],
    }
    assert first["nextCursor"] == "2"
    assert second["boundedDetails"]["items"] == [
        {"name": "C", "type": "EMPTY"},
        {"name": "D", "type": "ARMATURE"},
    ]
    assert second["nextCursor"] is None
    assert first["evidenceIdentity"] == second["evidenceIdentity"] == expected_identity
    assert render_calls == []


@pytest.mark.parametrize("section", ["GEOMETRY", "DEFORMATION"])
def test_inspect_returns_aggregate_sections_as_one_bounded_item(tmp_path: Path, section: str):
    store, *_ = _registered_prepare(tmp_path)
    service = PreparedObservationService(store, renderer=None)

    result = service.inspect("prepare-a", section, limit=1)

    assert result["section"] == section
    assert result["boundedDetails"]["totalCount"] == 1
    assert result["boundedDetails"]["summary"] == {}
    assert len(result["boundedDetails"]["items"]) == 1
    assert result["nextCursor"] is None


def test_inspect_materials_pages_only_cached_material_items(tmp_path: Path):
    store, *_ = _registered_prepare(tmp_path)
    service = PreparedObservationService(store, renderer=None)

    result = service.inspect("prepare-a", "MATERIALS", cursor="1", limit=1)

    assert result["boundedDetails"] == {
        "summary": {"materialCount": 3, "materialWorkflow": "PBR"},
        "totalCount": 3,
        "items": [{"name": "MatB", "usesNodes": True}],
    }
    assert result["nextCursor"] == "2"


@pytest.mark.parametrize(
    ("section", "cursor", "limit", "code"),
    [
        ("UNKNOWN", None, 10, "PREPARED_OBSERVATION_INVALID_SECTION"),
        ("STRUCTURE", "not-an-offset", 10, "PREPARED_OBSERVATION_INVALID_CURSOR"),
        ("STRUCTURE", None, 0, "PREPARED_OBSERVATION_INVALID_LIMIT"),
        ("STRUCTURE", None, 101, "PREPARED_OBSERVATION_INVALID_LIMIT"),
    ],
)
def test_inspect_rejects_invalid_pagination_inputs(
    tmp_path: Path, section: str, cursor: str | None, limit: int, code: str
):
    store, *_ = _registered_prepare(tmp_path)
    service = PreparedObservationService(store, renderer=None)

    with pytest.raises(PreparedObservationError) as exc_info:
        service.inspect("prepare-a", section, cursor=cursor, limit=limit)

    assert exc_info.value.code == code


def test_render_replay_reuses_same_artifact_and_never_reopens_changed_original(tmp_path: Path):
    store, _workspace, retained, main = _registered_prepare(tmp_path)
    original = tmp_path / "original.blend"
    original.write_bytes(retained.read_bytes())
    render_calls = []

    async def renderer(*, source_path: Path, output_path: Path, view: str, prepare_id: str):
        render_calls.append((source_path, view, prepare_id, source_path.read_bytes()))
        output_path.write_bytes(b"\x89PNG\r\n\x1a\n" + source_path.read_bytes() + view.encode())
        return {"openMs": 1.0, "renderMs": 2.0, "totalMs": 3.0}

    service = PreparedObservationService(store, renderer=renderer)
    original.write_bytes(b"BLENDER-original-changed-after-prepare")

    async def run():
        first = await service.render("prepare-a", "FRONT", "request-1")
        second = await service.render("prepare-a", "FRONT", "request-1")
        return first, second

    first, second = asyncio.run(run())

    assert len(render_calls) == 1
    assert render_calls[0][0] == retained.resolve()
    assert render_calls[0][3] != original.read_bytes()
    assert first == second
    assert first["view"] == "FRONT"
    assert first["supplementalArtifact"]["artifactId"] == second["supplementalArtifact"]["artifactId"]
    assert first["supplementalArtifact"]["kind"] == "PREVIEW"
    assert first["evidenceIdentity"]["sha256"] == _sha256(retained.read_bytes())
    assert first["supplementalRequestCount"] == 1
    assert store.resolve(first["supplementalArtifact"]["artifactId"]).is_file()
    assert main.read_bytes() == b"\x89PNG\r\n\x1a\nmain"


def test_rendering_one_prepare_does_not_touch_another_ready_item(tmp_path: Path):
    store_a, *_ = _registered_prepare(tmp_path / "a", prepare_id="prepare-a", item_key="item-a")
    store_b, *_ = _registered_prepare(tmp_path / "b", prepare_id="prepare-b", item_key="item-b")
    calls = {"prepare-a": 0, "prepare-b": 0}

    async def renderer(*, source_path: Path, output_path: Path, view: str, prepare_id: str):
        calls[prepare_id] += 1
        output_path.write_bytes(b"\x89PNG\r\n\x1a\n" + prepare_id.encode())
        return {"totalMs": 1.0}

    service_a = PreparedObservationService(store_a, renderer=renderer)
    PreparedObservationService(store_b, renderer=renderer)

    asyncio.run(service_a.render("prepare-a", "LEFT", "request-a"))

    assert calls == {"prepare-a": 1, "prepare-b": 0}


@pytest.mark.parametrize("view", ["MAIN", "THREE_QUARTER", "BOTTOM", "front"])
def test_render_rejects_invalid_supplemental_view(tmp_path: Path, view: str):
    store, *_ = _registered_prepare(tmp_path)
    service = PreparedObservationService(store, renderer=None)

    with pytest.raises(PreparedObservationError) as exc_info:
        asyncio.run(service.render("prepare-a", view, "request-1"))

    assert exc_info.value.code == "PREPARED_OBSERVATION_INVALID_VIEW"


def test_expired_prepare_rejects_inspect_and_render_with_stable_code(tmp_path: Path):
    clock = _Clock()
    store, *_ = _registered_prepare(tmp_path, clock=clock)
    service = PreparedObservationService(store, renderer=None)
    clock.advance(6)

    with pytest.raises(PreparedObservationError) as inspect_exc:
        service.inspect("prepare-a", "STRUCTURE")
    assert inspect_exc.value.code == "PREPARED_ARTIFACT_EXPIRED"

    with pytest.raises(PreparedObservationError) as render_exc:
        asyncio.run(service.render("prepare-a", "TOP", "request-1"))
    assert render_exc.value.code == "PREPARED_ARTIFACT_EXPIRED"


def test_cancelled_render_releases_lease_after_cleanup_was_blocked(tmp_path: Path):
    clock = _Clock()
    store, workspace, *_ = _registered_prepare(tmp_path, clock=clock)
    started = asyncio.Event()

    async def renderer(**_kwargs):
        started.set()
        await asyncio.Future()

    service = PreparedObservationService(store, renderer=renderer)

    async def run():
        task = asyncio.create_task(service.render("prepare-a", "RIGHT", "request-cancel"))
        await started.wait()
        clock.advance(6)
        assert store.cleanup_expired() == []
        assert workspace.is_dir()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert store.cleanup_expired() == ["prepare-a"]
    assert not workspace.exists()


def test_inspect_mcp_tool_returns_cached_structured_content_without_gui_or_worker(
    monkeypatch, tmp_path: Path
):
    from mcp.types import CallToolResult, TextContent
    from blender_mcp import server

    store, *_ = _registered_prepare(tmp_path)
    service = PreparedObservationService(store, renderer=None)

    monkeypatch.setattr(server, "get_prepared_observation_service", lambda: service, raising=False)
    monkeypatch.setattr(
        server,
        "get_blender_connection",
        lambda: (_ for _ in ()).throw(AssertionError("inspect must not touch GUI socket")),
    )
    monkeypatch.setattr(
        server,
        "prepare_blend_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("inspect must not spawn prepare worker")
        ),
    )

    result = asyncio.run(
        server.mcp.call_tool(
            "inspect_prepared_asset",
            {"prepare_id": "prepare-a", "section": "STRUCTURE", "limit": 2},
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent["prepareId"] == "prepare-a"
    assert result.structuredContent["itemKey"] == "item-a"
    assert result.structuredContent["boundedDetails"]["items"][0]["name"] == "A"
    assert len(result.content) == 1
    assert isinstance(result.content[0], TextContent)
    assert "item-a" in result.content[0].text
    assert "STRUCTURE" in result.content[0].text


def test_render_mcp_tool_returns_matching_item_label_and_image(monkeypatch, tmp_path: Path):
    import base64
    from mcp.types import CallToolResult, ImageContent, TextContent
    from blender_mcp import server

    store, *_ = _registered_prepare(tmp_path)

    async def renderer(*, source_path: Path, output_path: Path, view: str, prepare_id: str):
        output_path.write_bytes(b"\x89PNG\r\n\x1a\nrendered-" + view.encode())
        return {"openMs": 1.0, "renderMs": 2.0, "totalMs": 3.0}

    service = PreparedObservationService(store, renderer=renderer)
    monkeypatch.setattr(server, "get_prepared_observation_service", lambda: service, raising=False)
    monkeypatch.setattr(server, "get_prepared_artifact_store", lambda: store)

    result = asyncio.run(
        server.mcp.call_tool(
            "render_prepared_asset_view",
            {
                "prepare_id": "prepare-a",
                "view": "TOP",
                "idempotency_key": "request-top",
            },
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent["prepareId"] == "prepare-a"
    assert result.structuredContent["itemKey"] == "item-a"
    assert result.structuredContent["view"] == "TOP"
    assert len(result.content) == 2
    assert isinstance(result.content[0], TextContent)
    assert "item-a" in result.content[0].text
    assert "TOP" in result.content[0].text
    assert isinstance(result.content[1], ImageContent)
    assert base64.b64decode(result.content[1].data).startswith(b"\x89PNG\r\n\x1a\nrendered-TOP")

"""Batch asset preparation job manager and MCP adapter tests."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import time
from typing import Any

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from blender_mcp import asset_pipeline
from blender_mcp.blender_runtime import ASSET_CONCURRENCY_ENV, BlenderRuntime
from blender_mcp.file_transfer import FileTransferError, PreparedArtifactTransferService
from blender_mcp.prepared_artifacts import PreparedArtifactStore


PNG_BYTES = b"\x89PNG\r\n\x1a\nbatch-main-preview"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source(path: Path, payload: bytes) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return {
        "itemKey": f"item-{path.stem}",
        "path": str(path),
        "sourceFingerprint": _sha256(path),
    }


def _ready_runner(store: PreparedArtifactStore, calls: list[str] | None = None):
    async def run(
        source_path: str | Path,
        *,
        profile: str,
        runtime: BlenderRuntime | None,
        prepare_id: str,
        output_dir: str | Path,
        artifact_store: PreparedArtifactStore,
        expected_source_fingerprint: str,
    ) -> dict[str, Any]:
        source = Path(source_path)
        if calls is not None:
            calls.append(source.name)
        assert artifact_store is store
        assert runtime is not None
        assert expected_source_fingerprint == _sha256(source)
        workspace = Path(output_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        retained = workspace / f"source-{prepare_id}.blend"
        retained.write_bytes(source.read_bytes())
        preview = workspace / "main.png"
        preview.write_bytes(PNG_BYTES)
        cache = workspace / "observation-cache.json"
        cache.write_text("{}", encoding="utf-8")
        store.register_workspace(
            prepare_id,
            workspace,
            retained_source_path=retained,
            observation_cache_path=cache,
        )
        ref = store.register_artifact(
            prepare_id,
            "PREVIEW",
            preview,
            content_type="image/png",
        )
        return {
            "status": "READY",
            "prepareId": prepare_id,
            "profile": profile,
            "observation": {
                "prepareId": prepare_id,
                "source": {"displayName": source.name},
                "structure": {"objectCount": 1},
            },
            "previewPath": str(preview),
            "artifacts": [
                {
                    "artifact_id": ref.artifact_id,
                    "kind": ref.kind,
                    "size": ref.size,
                    "sha256": ref.sha256,
                    "content_type": ref.content_type,
                    "expires_at": ref.expires_at,
                }
            ],
            "warnings": [],
            "timings": {"totalMs": 1.0},
        }

    return run


async def _wait_terminal(manager, batch_prepare_id: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while True:
        page = manager.get_prepare_blend_assets(batch_prepare_id, limit=100)
        if page["status"] in {"READY", "PARTIAL", "FAILED", "CANCELLED"}:
            return page
        if time.monotonic() >= deadline:
            raise AssertionError(f"batch did not reach terminal state: {page}")
        await asyncio.sleep(0.005)


def test_discover_blend_files_is_recursive_deterministic_and_fingerprinted(tmp_path: Path):
    root = tmp_path / "library"
    first = root / "A" / "chair.blend"
    second = root / "b" / "Lamp.BLEND"
    ignored = root / "notes.txt"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"BLENDER-chair")
    second.write_bytes(b"BLENDER-lamp")
    ignored.write_text("ignore me", encoding="utf-8")

    discovered = asset_pipeline.discover_blend_files(root)
    replay = asset_pipeline.discover_blend_files(root)

    assert discovered == replay
    assert [item["sourceDisplayName"] for item in discovered] == ["chair.blend", "Lamp.BLEND"]
    assert len({item["itemKey"] for item in discovered}) == 2
    assert all(item["itemKey"].startswith("blend-") for item in discovered)
    assert [Path(item["path"]) for item in discovered] == [first.resolve(), second.resolve()]
    assert [item["sourceFingerprint"] for item in discovered] == [_sha256(first), _sha256(second)]
    assert all(len(item["sourceFingerprint"]) == 64 for item in discovered)

    old_key = discovered[0]["itemKey"]
    old_fingerprint = discovered[0]["sourceFingerprint"]
    first.write_bytes(b"BLENDER-chair-changed")
    changed = asset_pipeline.discover_blend_files(root)
    assert changed[0]["itemKey"] == old_key
    assert changed[0]["sourceFingerprint"] != old_fingerprint


def test_batch_start_validation_and_concurrency_contract(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(asset_pipeline.os, "cpu_count", lambda: 8)
    monkeypatch.delenv(ASSET_CONCURRENCY_ENV, raising=False)
    store = PreparedArtifactStore(ttl_seconds=60)
    manager = asset_pipeline.AssetPipelineManager(
        artifact_store=store,
        runtime_provider=lambda: BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        prepare_runner=_ready_runner(store),
    )
    item_a = _source(tmp_path / "a.blend", b"BLENDER-a")
    item_b = _source(tmp_path / "b.blend", b"BLENDER-b")

    async def scenario():
        with pytest.raises(asset_pipeline.AssetBatchError) as too_small:
            await manager.start_prepare_blend_assets([item_a], "METADATA", "one")
        assert too_small.value.code == "BATCH_PREPARE_MIN_ITEMS"

        duplicate = dict(item_b)
        duplicate["itemKey"] = item_a["itemKey"]
        with pytest.raises(asset_pipeline.AssetBatchError) as duplicate_error:
            await manager.start_prepare_blend_assets([item_a, duplicate], "METADATA", "duplicate")
        assert duplicate_error.value.code == "BATCH_PREPARE_DUPLICATE_ITEM_KEY"

        default = await manager.start_prepare_blend_assets([item_a, item_b], "METADATA", "default")
        assert default["concurrency"] == 4
        await _wait_terminal(manager, default["batchPrepareId"])

        monkeypatch.setenv(ASSET_CONCURRENCY_ENV, "12")
        for requested, expected in [(1, 1), (4, 4), (12, 12), (99, 12)]:
            result = await manager.start_prepare_blend_assets(
                [item_a, item_b],
                "METADATA",
                f"explicit-{requested}",
                concurrency=requested,
            )
            assert result["concurrency"] == expected
            await _wait_terminal(manager, result["batchPrepareId"])

        await manager.shutdown()

    asyncio.run(scenario())


def test_ready_items_are_pageable_before_batch_finishes_and_failure_is_isolated(tmp_path: Path):
    store = PreparedArtifactStore(ttl_seconds=60)
    release = asyncio.Event()
    ready = _ready_runner(store)

    async def runner(source_path, **kwargs):
        name = Path(source_path).name
        if name == "c.blend":
            raise asset_pipeline.AssetPrepareError("BLENDER_PREPARE_FAILED: FIXTURE: broken file")
        if name == "b.blend":
            await release.wait()
        return await ready(source_path, **kwargs)

    manager = asset_pipeline.AssetPipelineManager(
        artifact_store=store,
        runtime_provider=lambda: BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        prepare_runner=runner,
    )
    items = [
        _source(tmp_path / "a.blend", b"BLENDER-a"),
        _source(tmp_path / "b.blend", b"BLENDER-b"),
        _source(tmp_path / "c.blend", b"BLENDER-c"),
    ]

    async def scenario():
        started = await manager.start_prepare_blend_assets(items, "METADATA", "mixed", concurrency=3)
        assert started["status"] in {"QUEUED", "RUNNING"}

        deadline = time.monotonic() + 1.0
        page = None
        while time.monotonic() < deadline:
            page = manager.get_prepare_blend_assets(started["batchPrepareId"], limit=3)
            states = {item["itemKey"]: item["status"] for item in page["items"]}
            if states[items[0]["itemKey"]] == "READY" and states[items[2]["itemKey"]] == "FAILED":
                break
            await asyncio.sleep(0.005)
        assert page is not None
        assert page["status"] == "RUNNING"
        assert states[items[1]["itemKey"]] in {"PENDING", "RUNNING"}
        assert page["items"][0]["result"]["observation"]["source"]["displayName"] == "a.blend"
        assert page["items"][2]["error"]["code"] == "BLENDER_PREPARE_FAILED"

        release.set()
        terminal = await _wait_terminal(manager, started["batchPrepareId"])
        assert terminal["status"] == "PARTIAL"
        assert terminal["counts"]["READY"] == 2
        assert terminal["counts"]["FAILED"] == 1
        await manager.shutdown()

    asyncio.run(scenario())


def test_two_batches_share_one_global_worker_cap(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(asset_pipeline.os, "cpu_count", lambda: 8)
    monkeypatch.setenv(ASSET_CONCURRENCY_ENV, "2")
    store = PreparedArtifactStore(ttl_seconds=60)
    active = 0
    max_active = 0
    lock = asyncio.Lock()
    ready = _ready_runner(store)

    async def runner(source_path, **kwargs):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.03)
            return await ready(source_path, **kwargs)
        finally:
            async with lock:
                active -= 1

    manager = asset_pipeline.AssetPipelineManager(
        artifact_store=store,
        runtime_provider=lambda: BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        prepare_runner=runner,
    )
    first = [_source(tmp_path / "one" / f"{i}.blend", f"BLENDER-one-{i}".encode()) for i in range(3)]
    second = [_source(tmp_path / "two" / f"{i}.blend", f"BLENDER-two-{i}".encode()) for i in range(3)]

    async def scenario():
        batch_a = await manager.start_prepare_blend_assets(first, "METADATA", "batch-a", concurrency=12)
        batch_b = await manager.start_prepare_blend_assets(second, "METADATA", "batch-b", concurrency=12)
        await asyncio.gather(
            _wait_terminal(manager, batch_a["batchPrepareId"]),
            _wait_terminal(manager, batch_b["batchPrepareId"]),
        )
        await manager.shutdown()

    asyncio.run(scenario())
    assert max_active == 2


def test_changed_source_fingerprint_fails_only_that_item(tmp_path: Path):
    store = PreparedArtifactStore(ttl_seconds=60)
    manager = asset_pipeline.AssetPipelineManager(
        artifact_store=store,
        runtime_provider=lambda: BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        prepare_runner=_ready_runner(store),
    )
    item_a = _source(tmp_path / "a.blend", b"BLENDER-a-original")
    item_b = _source(tmp_path / "b.blend", b"BLENDER-b")
    Path(item_a["path"]).write_bytes(b"BLENDER-a-replaced")

    async def scenario():
        started = await manager.start_prepare_blend_assets([item_a, item_b], "METADATA", "fingerprint")
        terminal = await _wait_terminal(manager, started["batchPrepareId"])
        assert terminal["status"] == "PARTIAL"
        by_key = {item["itemKey"]: item for item in terminal["items"]}
        assert by_key[item_a["itemKey"]]["status"] == "FAILED"
        assert by_key[item_a["itemKey"]]["error"]["code"] == "BATCH_PREPARE_SOURCE_CHANGED"
        assert by_key[item_b["itemKey"]]["status"] == "READY"
        await manager.shutdown()

    asyncio.run(scenario())


def test_retry_replays_request_validates_membership_and_does_not_regenerate_ready_item(tmp_path: Path):
    store = PreparedArtifactStore(ttl_seconds=60)
    attempts: dict[str, int] = {}
    calls: list[str] = []
    ready = _ready_runner(store, calls)

    async def runner(source_path, **kwargs):
        name = Path(source_path).name
        attempts[name] = attempts.get(name, 0) + 1
        if name == "a.blend" and attempts[name] == 1:
            raise asset_pipeline.AssetPrepareError("BLENDER_PREPARE_FAILED: FIXTURE: first attempt")
        return await ready(source_path, **kwargs)

    manager = asset_pipeline.AssetPipelineManager(
        artifact_store=store,
        runtime_provider=lambda: BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        prepare_runner=runner,
    )
    item_a = _source(tmp_path / "a.blend", b"BLENDER-a")
    item_b = _source(tmp_path / "b.blend", b"BLENDER-b")

    async def scenario():
        first = await manager.start_prepare_blend_assets([item_a, item_b], "METADATA", "request-1")
        batch_id = first["batchPrepareId"]
        terminal = await _wait_terminal(manager, batch_id)
        assert terminal["status"] == "PARTIAL"
        calls_after_first = list(calls)

        replay = await manager.start_prepare_blend_assets([item_a, item_b], "METADATA", "request-1")
        assert replay["batchPrepareId"] == batch_id
        await asyncio.sleep(0.02)
        assert calls == calls_after_first

        retried = await manager.start_prepare_blend_assets(
            [item_a, item_b],
            "METADATA",
            "request-2",
            batch_prepare_id=batch_id,
        )
        assert retried["batchPrepareId"] == batch_id
        final = await _wait_terminal(manager, batch_id)
        assert final["status"] == "READY"
        assert attempts["a.blend"] == 2
        assert attempts["b.blend"] == 1

        unknown = dict(item_a)
        unknown["itemKey"] = "not-in-manifest"
        with pytest.raises(asset_pipeline.AssetBatchError) as membership:
            await manager.start_prepare_blend_assets(
                [unknown], "METADATA", "request-3", batch_prepare_id=batch_id
            )
        assert membership.value.code == "BATCH_PREPARE_ITEM_NOT_IN_MANIFEST"

        changed = dict(item_a)
        changed["sourceFingerprint"] = "0" * 64
        with pytest.raises(asset_pipeline.AssetBatchError) as fingerprint:
            await manager.start_prepare_blend_assets(
                [changed], "METADATA", "request-4", batch_prepare_id=batch_id
            )
        assert fingerprint.value.code == "BATCH_PREPARE_SOURCE_FINGERPRINT_MISMATCH"
        await manager.shutdown()

    asyncio.run(scenario())


def test_cancel_stops_running_items_and_cleans_unregistered_workspace(monkeypatch, tmp_path: Path):
    store = PreparedArtifactStore(ttl_seconds=60)
    started = asyncio.Event()
    workspace = tmp_path / "cancel-workspace"
    created = False

    def fake_mkdtemp(*_args, **_kwargs):
        nonlocal created
        if not created:
            created = True
            workspace.mkdir(parents=True, exist_ok=True)
            return str(workspace)
        other = tmp_path / "cancel-workspace-2"
        other.mkdir(parents=True, exist_ok=True)
        return str(other)

    monkeypatch.setattr(asset_pipeline.tempfile, "mkdtemp", fake_mkdtemp)

    async def runner(_source_path, **kwargs):
        Path(kwargs["output_dir"]).joinpath("worker-started.txt").write_text("started", encoding="utf-8")
        started.set()
        await asyncio.Future()

    manager = asset_pipeline.AssetPipelineManager(
        artifact_store=store,
        runtime_provider=lambda: BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        prepare_runner=runner,
    )
    items = [
        _source(tmp_path / "a.blend", b"BLENDER-a"),
        _source(tmp_path / "b.blend", b"BLENDER-b"),
    ]

    async def scenario():
        result = await manager.start_prepare_blend_assets(items, "METADATA", "cancel", concurrency=1)
        await asyncio.wait_for(started.wait(), 1.0)
        cancelled = await manager.cancel_prepare_blend_assets(result["batchPrepareId"])
        assert cancelled["status"] == "CANCELLED"
        assert all(item["status"] == "CANCELLED" for item in cancelled["items"])
        await manager.shutdown()

    asyncio.run(scenario())
    assert not workspace.exists()


def test_upload_service_can_enforce_batch_prepare_manifest_membership(tmp_path: Path):
    store = PreparedArtifactStore(ttl_seconds=60)
    manager = asset_pipeline.AssetPipelineManager(
        artifact_store=store,
        runtime_provider=lambda: BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        prepare_runner=_ready_runner(store),
    )
    items = [
        _source(tmp_path / "a.blend", b"BLENDER-a"),
        _source(tmp_path / "b.blend", b"BLENDER-b"),
    ]

    async def scenario():
        started = await manager.start_prepare_blend_assets(items, "PUBLISH", "manifest")
        terminal = await _wait_terminal(manager, started["batchPrepareId"])
        first = terminal["items"][0]
        artifact_id = first["result"]["artifacts"][0]["artifact_id"]
        service = PreparedArtifactTransferService(
            store,
            prepare_manifest_resolver=manager.resolve_upload_manifest,
        )
        with pytest.raises(FileTransferError) as wrong_item:
            await service.start_upload_prepared_artifacts(
                started["batchPrepareId"],
                [
                    {
                        "itemKey": "wrong-item",
                        "artifactId": artifact_id,
                        "method": "PUT",
                        "url": "http://127.0.0.1:1/not-used",
                    }
                ],
                "upload-membership",
            )
        assert wrong_item.value.code == "UPLOAD_ARTIFACT_NOT_IN_BATCH_PREPARE"
        await manager.shutdown()

    asyncio.run(scenario())


def test_batch_mcp_get_returns_item_labels_and_adjacent_images(monkeypatch, tmp_path: Path):
    from blender_mcp import server

    preview_a = tmp_path / "a.png"
    preview_b = tmp_path / "b.png"
    preview_a.write_bytes(PNG_BYTES + b"a")
    preview_b.write_bytes(PNG_BYTES + b"b")

    class FakeManager:
        def get_prepare_blend_assets(self, batch_prepare_id, cursor=None, limit=None):
            assert batch_prepare_id == "batch-1"
            return {
                "batchPrepareId": "batch-1",
                "status": "READY",
                "counts": {"PENDING": 0, "RUNNING": 0, "READY": 2, "FAILED": 0, "CANCELLED": 0},
                "items": [
                    {
                        "itemKey": "item-a",
                        "sourceDisplayName": "a.blend",
                        "status": "READY",
                        "result": {
                            "prepareId": "prepare-a",
                            "observation": {"structure": {"objectCount": 1}},
                            "artifacts": [{"artifact_id": "preview-a", "kind": "PREVIEW", "size": 1, "sha256": "a" * 64, "content_type": "image/png", "expires_at": "later"}],
                            "timings": {"totalMs": 1.0},
                            "warnings": [],
                        },
                    },
                    {
                        "itemKey": "item-b",
                        "sourceDisplayName": "b.blend",
                        "status": "READY",
                        "result": {
                            "prepareId": "prepare-b",
                            "observation": {"structure": {"objectCount": 2}},
                            "artifacts": [{"artifact_id": "preview-b", "kind": "PREVIEW", "size": 1, "sha256": "b" * 64, "content_type": "image/png", "expires_at": "later"}],
                            "timings": {"totalMs": 1.0},
                            "warnings": [],
                        },
                    },
                ],
                "nextCursor": None,
            }

    class FakeStore:
        def resolve(self, artifact_id: str) -> Path:
            return {"preview-a": preview_a, "preview-b": preview_b}[artifact_id]

    monkeypatch.setattr(server, "get_asset_pipeline_manager", lambda: FakeManager(), raising=False)
    monkeypatch.setattr(server, "get_prepared_artifact_store", lambda: FakeStore())

    result = asyncio.run(
        server.mcp.call_tool(
            "get_prepare_blend_assets",
            {"batch_prepare_id": "batch-1", "limit": 10},
        )
    )
    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent["items"][0]["itemKey"] == "item-a"
    assert result.structuredContent["items"][0]["result"]["artifacts"][0]["artifactId"] == "preview-a"
    assert [type(block) for block in result.content] == [TextContent, TextContent, ImageContent, TextContent, ImageContent]
    assert result.content[1].text == "item-a — a.blend"
    assert result.content[3].text == "item-b — b.blend"


def test_batch_status_mode_is_lightweight_and_keeps_ready_identity(tmp_path: Path):
    store = PreparedArtifactStore(ttl_seconds=60)
    manager = asset_pipeline.AssetPipelineManager(
        artifact_store=store,
        runtime_provider=lambda: BlenderRuntime("C:/Blender/blender.exe", "ENV"),
        prepare_runner=_ready_runner(store),
    )
    items = [
        _source(tmp_path / "status-a.blend", b"BLENDER-status-a"),
        _source(tmp_path / "status-b.blend", b"BLENDER-status-b"),
    ]

    async def scenario():
        started = await manager.start_prepare_blend_assets(items, "PUBLISH", "status-lightweight")
        terminal = await _wait_terminal(manager, started["batchPrepareId"])
        full_by_key = {item["itemKey"]: item for item in terminal["items"]}

        status_page = manager.get_prepare_blend_assets(
            started["batchPrepareId"],
            limit=10,
            mode="STATUS",
        )

        assert status_page["status"] == "READY"
        assert status_page["counts"]["READY"] == 2
        for item in status_page["items"]:
            assert "result" not in item
            assert item["prepareId"] == full_by_key[item["itemKey"]]["result"]["prepareId"]
            assert item["timings"]["totalMs"] == 1.0
        await manager.shutdown()

    asyncio.run(scenario())


def test_batch_mcp_status_mode_returns_no_preview_images(monkeypatch):
    from blender_mcp import server

    class FakeManager:
        def get_prepare_blend_assets(self, batch_prepare_id, cursor=None, limit=None, mode=None):
            assert batch_prepare_id == "batch-status"
            assert mode == "STATUS"
            return {
                "batchPrepareId": "batch-status",
                "status": "READY",
                "mode": "STATUS",
                "counts": {"PENDING": 0, "RUNNING": 0, "READY": 1, "FAILED": 0, "CANCELLED": 0},
                "items": [
                    {
                        "itemKey": "item-ready",
                        "sourceDisplayName": "ready.blend",
                        "sourceFingerprint": "a" * 64,
                        "status": "READY",
                        "attempts": 1,
                        "prepareId": "prepare-ready",
                        "timings": {"totalMs": 12.5},
                    }
                ],
                "nextCursor": None,
            }

    monkeypatch.setattr(server, "get_asset_pipeline_manager", lambda: FakeManager(), raising=False)

    result = asyncio.run(
        server.mcp.call_tool(
            "get_prepare_blend_assets",
            {"batch_prepare_id": "batch-status", "limit": 10, "mode": "STATUS"},
        )
    )

    assert isinstance(result, CallToolResult)
    assert result.isError is False
    assert result.structuredContent["items"][0]["prepareId"] == "prepare-ready"
    assert "result" not in result.structuredContent["items"][0]
    assert [type(block) for block in result.content] == [TextContent]


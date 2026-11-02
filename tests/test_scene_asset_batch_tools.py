from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from blender_mcp import server
from blender_mcp.asset_pipeline import AssetPipelineManager


def _scene_roots():
    return {
        "sceneFingerprint": "a" * 64,
        "sceneName": "Scene",
        "sourceBlendPath": "C:/projects/source.blend",
        "blenderVersion": "5.2.0",
        "sceneObjectCount": 6,
        "roots": [
            {
                "rootObjectId": "obj_000000000000000000000001",
                "objectName": "DeskRoot",
                "objectType": "EMPTY",
                "dataName": None,
                "collectionNames": ["Furniture"],
                "descendantObjectIds": ["obj_000000000000000000000004"],
                "descendantObjectNames": ["DeskTop"],
                "objectCount": 2,
                "geometryObjectCount": 1,
            },
            {
                "rootObjectId": "obj_000000000000000000000002",
                "objectName": "Lamp",
                "objectType": "MESH",
                "dataName": "LampMesh",
                "collectionNames": ["Lighting"],
                "descendantObjectIds": [],
                "descendantObjectNames": [],
                "objectCount": 1,
                "geometryObjectCount": 1,
            },
        ],
    }


class _FakePipelineManager:
    def __init__(self):
        self.calls = []

    async def start_prepare_blend_assets(
        self,
        items,
        profile,
        idempotency_key,
        *,
        batch_prepare_id=None,
        concurrency=None,
    ):
        self.calls.append(
            {
                "items": items,
                "profile": profile,
                "idempotency_key": idempotency_key,
                "batch_prepare_id": batch_prepare_id,
                "concurrency": concurrency,
            }
        )
        return {
            "batchPrepareId": batch_prepare_id or "batch-scene-1",
            "status": "RUNNING",
            "itemCount": len(items),
            "concurrency": concurrency or 2,
        }


def _items():
    return [
        server.SceneAssetBatchItem(
            asset_key="desk",
            root_object_ids=["obj_000000000000000000000001"],
            display_name="办公桌",
        ),
        server.SceneAssetBatchItem(
            asset_key="lamp",
            root_object_ids=["obj_000000000000000000000002"],
            display_name="台灯",
        ),
    ]


def test_start_prepare_scene_assets_creates_one_snapshot_per_logical_asset(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(server, "_SCENE_BATCH_ROOT", tmp_path / "scene-batches")
    monkeypatch.setattr(server, "_request_scene_asset_roots", _scene_roots)
    manager = _FakePipelineManager()
    monkeypatch.setattr(server, "get_asset_pipeline_manager", lambda: manager)
    server._scene_batch_manifest_paths.clear()
    snapshot_calls = []

    def create_snapshot(
        filepath,
        *,
        closure_mode="ASSET_CLOSURE",
        root_object_names=None,
        expected_scene_fingerprint=None,
    ):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = ("BLEND:" + ",".join(root_object_names or [])).encode("utf-8")
        path.write_bytes(payload)
        snapshot_calls.append(
            {
                "filepath": path,
                "root_object_names": root_object_names,
                "expected_scene_fingerprint": expected_scene_fingerprint,
                "closure_mode": closure_mode,
            }
        )
        return {
            "filepath": str(path),
            "rootObjectNames": root_object_names,
            "sourceSceneFingerprint": expected_scene_fingerprint,
        }

    monkeypatch.setattr(server, "create_blend_snapshot", create_snapshot)

    result = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=_items(),
            idempotency_key="scene-request-1",
            concurrency=2,
        )
    )

    assert result.ok is True
    assert result.batch_prepare_id == "batch-scene-1"
    assert result.item_count == 2
    assert [item.item_key for item in result.items] == ["desk", "lamp"]
    assert [item.source_display_name for item in result.items] == ["办公桌", "台灯"]
    assert all(len(item.source_fingerprint) == 64 for item in result.items)
    assert [item.root_object_names for item in result.items] == [["DeskRoot"], ["Lamp"]]
    serialized = result.model_dump(mode="json", by_alias=True)
    assert serialized["items"][0]["itemKey"] == "desk"
    assert serialized["items"][0]["sourceDisplayName"] == "办公桌"
    assert serialized["items"][0]["sourceFingerprint"] == result.items[0].source_fingerprint
    assert len(snapshot_calls) == 2
    assert [call["root_object_names"] for call in snapshot_calls] == [
        ["DeskRoot"],
        ["Lamp"],
    ]
    prepared_items = manager.calls[0]["items"]
    assert [item["itemKey"] for item in prepared_items] == ["desk", "lamp"]
    for item in prepared_items:
        path = Path(item["path"])
        assert path.is_file()
        assert item["sourceFingerprint"] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_scene_batch_idempotent_replay_reuses_the_same_snapshots(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(server, "_SCENE_BATCH_ROOT", tmp_path / "scene-batches")
    monkeypatch.setattr(server, "_request_scene_asset_roots", _scene_roots)
    manager = _FakePipelineManager()
    monkeypatch.setattr(server, "get_asset_pipeline_manager", lambda: manager)
    server._scene_batch_manifest_paths.clear()
    snapshot_calls = []

    def create_snapshot(filepath, **kwargs):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("snapshot-" + path.stem).encode("utf-8"))
        snapshot_calls.append(path)
        return {
            "filepath": str(path),
            "sourceSceneFingerprint": kwargs["expected_scene_fingerprint"],
        }

    monkeypatch.setattr(server, "create_blend_snapshot", create_snapshot)

    first = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=_items(),
            idempotency_key="scene-request-replay",
        )
    )
    second = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=_items(),
            idempotency_key="scene-request-replay",
        )
    )

    assert first.batch_prepare_id == second.batch_prepare_id
    assert len(snapshot_calls) == 2
    assert len(manager.calls) == 2
    assert [item["path"] for item in manager.calls[0]["items"]] == [
        item["path"] for item in manager.calls[1]["items"]
    ]


def test_scene_batch_rejects_duplicate_root_assignment_before_snapshot(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(server, "_SCENE_BATCH_ROOT", tmp_path / "scene-batches")
    monkeypatch.setattr(server, "_request_scene_asset_roots", _scene_roots)
    manager = _FakePipelineManager()
    monkeypatch.setattr(server, "get_asset_pipeline_manager", lambda: manager)
    snapshot_calls = []
    monkeypatch.setattr(
        server,
        "create_blend_snapshot",
        lambda *args, **kwargs: snapshot_calls.append((args, kwargs)),
    )

    items = [
        server.SceneAssetBatchItem(asset_key="first", root_object_ids=["obj_000000000000000000000001"]),
        server.SceneAssetBatchItem(asset_key="second", root_object_ids=["obj_000000000000000000000001"]),
    ]
    result = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=items,
            idempotency_key="duplicate-root-request",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "SCENE_BATCH_DUPLICATE_ROOT_OBJECT"
    assert snapshot_calls == []
    assert manager.calls == []


def test_scene_batch_rejects_scene_change_before_snapshot(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(server, "_SCENE_BATCH_ROOT", tmp_path / "scene-batches")
    roots = _scene_roots()
    roots["sceneFingerprint"] = "b" * 64
    monkeypatch.setattr(server, "_request_scene_asset_roots", lambda: roots)
    manager = _FakePipelineManager()
    monkeypatch.setattr(server, "get_asset_pipeline_manager", lambda: manager)
    snapshot_calls = []
    monkeypatch.setattr(
        server,
        "create_blend_snapshot",
        lambda *args, **kwargs: snapshot_calls.append((args, kwargs)),
    )

    result = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=_items(),
            idempotency_key="stale-scene-request",
        )
    )

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "SCENE_CHANGED"
    assert snapshot_calls == []
    assert manager.calls == []


def test_retry_scene_batch_reuses_immutable_snapshots_and_accepts_failed_subset(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(server, "_SCENE_BATCH_ROOT", tmp_path / "scene-batches")
    monkeypatch.setattr(server, "_request_scene_asset_roots", _scene_roots)
    manager = _FakePipelineManager()
    monkeypatch.setattr(server, "get_asset_pipeline_manager", lambda: manager)
    server._scene_batch_manifest_paths.clear()
    snapshot_calls = []

    def create_snapshot(filepath, **kwargs):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("snapshot-" + path.stem).encode("utf-8"))
        snapshot_calls.append(path)
        return {"sourceSceneFingerprint": kwargs["expected_scene_fingerprint"]}

    monkeypatch.setattr(server, "create_blend_snapshot", create_snapshot)

    first = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=_items(),
            idempotency_key="scene-request-retry",
        )
    )
    retry = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=[
                server.SceneAssetBatchItem(
                    asset_key="lamp",
                    root_object_ids=["obj_000000000000000000000002"],
                )
            ],
            idempotency_key="scene-request-retry-2",
            batch_prepare_id=first.batch_prepare_id,
        )
    )

    assert first.ok is True
    assert retry.ok is True
    assert retry.batch_prepare_id == first.batch_prepare_id
    assert len(snapshot_calls) == 2
    assert len(manager.calls) == 2
    assert len(manager.calls[1]["items"]) == 1
    assert manager.calls[1]["items"][0]["itemKey"] == "lamp"
    assert manager.calls[1]["items"][0]["sourceDisplayName"] == "台灯"
    assert retry.items[0].source_display_name == "台灯"
    assert manager.calls[1]["items"][0]["path"] == manager.calls[0]["items"][1]["path"]


def test_scene_batch_retry_rejects_changed_display_name(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(server, "_SCENE_BATCH_ROOT", tmp_path / "scene-batches")
    monkeypatch.setattr(server, "_request_scene_asset_roots", _scene_roots)
    manager = _FakePipelineManager()
    monkeypatch.setattr(server, "get_asset_pipeline_manager", lambda: manager)
    server._scene_batch_manifest_paths.clear()
    snapshot_calls = []

    def create_snapshot(filepath, **kwargs):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("snapshot-" + path.stem).encode("utf-8"))
        snapshot_calls.append(path)
        return {"sourceSceneFingerprint": kwargs["expected_scene_fingerprint"]}

    monkeypatch.setattr(server, "create_blend_snapshot", create_snapshot)
    first = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=_items(),
            idempotency_key="scene-request-display-name",
        )
    )
    retry = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=[
                server.SceneAssetBatchItem(
                    asset_key="desk",
                    root_object_ids=["obj_000000000000000000000001"],
                    display_name="改名后的办公桌",
                )
            ],
            idempotency_key="scene-request-display-name-retry",
            batch_prepare_id=first.batch_prepare_id,
        )
    )

    assert first.ok is True
    assert retry.ok is False
    assert retry.error is not None
    assert retry.error.code == "SCENE_BATCH_DISPLAY_NAME_MISMATCH"
    assert len(snapshot_calls) == 2
    assert len(manager.calls) == 1


def test_scene_batch_groups_multiple_roots_into_one_logical_asset(
    monkeypatch, tmp_path: Path
):
    roots = _scene_roots()
    roots["roots"].append(
        {
            "rootObjectId": "obj_000000000000000000000003",
            "objectName": "Chair",
            "objectType": "MESH",
            "dataName": "ChairMesh",
            "collectionNames": ["Furniture"],
            "descendantObjectIds": [],
            "descendantObjectNames": [],
            "objectCount": 1,
            "geometryObjectCount": 1,
        }
    )
    monkeypatch.setattr(server, "_SCENE_BATCH_ROOT", tmp_path / "scene-batches")
    monkeypatch.setattr(server, "_request_scene_asset_roots", lambda: roots)
    manager = _FakePipelineManager()
    monkeypatch.setattr(server, "get_asset_pipeline_manager", lambda: manager)
    server._scene_batch_manifest_paths.clear()
    snapshot_calls = []

    def create_snapshot(filepath, **kwargs):
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("snapshot-" + path.stem).encode("utf-8"))
        snapshot_calls.append(kwargs["root_object_names"])
        return {"sourceSceneFingerprint": kwargs["expected_scene_fingerprint"]}

    monkeypatch.setattr(server, "create_blend_snapshot", create_snapshot)
    items = [
        server.SceneAssetBatchItem(
            asset_key="desk_set",
            root_object_ids=["obj_000000000000000000000001", "obj_000000000000000000000002"],
            display_name="桌椅灯组合",
        ),
        server.SceneAssetBatchItem(asset_key="chair", root_object_ids=["obj_000000000000000000000003"]),
    ]

    result = asyncio.run(
        server.start_prepare_scene_assets(
            scene_fingerprint="a" * 64,
            items=items,
            idempotency_key="scene-request-grouped-roots",
        )
    )

    assert result.ok is True
    assert snapshot_calls == [["DeskRoot", "Lamp"], ["Chair"]]
    assert [item["sourceDisplayName"] for item in manager.calls[0]["items"]] == [
        "桌椅灯组合",
        "chair",
    ]


def test_scene_batch_item_schema_rejects_duplicate_roots_and_unsafe_asset_keys():
    with pytest.raises(ValidationError):
        server.SceneAssetBatchItem(asset_key="../outside", root_object_ids=["root-1"])
    with pytest.raises(ValidationError):
        server.SceneAssetBatchItem(asset_key="desk", root_object_ids=["root-1", "root-1"])
    with pytest.raises(ValidationError):
        server.SceneAssetBatchItem(asset_key="desk", root_object_ids=[""])
    with pytest.raises(ValidationError):
        server.SceneAssetBatchItem(asset_key="desk", root_object_ids=["obj_000000000000000000000001"], display_name="   ")


def test_orphan_scene_batch_cleanup_only_removes_its_managed_temp_root(
    monkeypatch, tmp_path: Path
):
    managed_root = tmp_path / "managed-scene-batches"
    orphan = managed_root / "old-request"
    orphan.mkdir(parents=True)
    (orphan / "snapshot.blend").write_bytes(b"orphan")
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(server, "_SCENE_BATCH_ROOT", managed_root)
    monkeypatch.setattr(server, "_scene_batch_startup_cleanup_done", False)

    server._cleanup_orphaned_scene_batch_snapshots()

    assert not orphan.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert server._scene_batch_startup_cleanup_done is True


def test_asset_pipeline_preserves_source_display_name_in_batch_identity(tmp_path: Path):
    manager = AssetPipelineManager.__new__(AssetPipelineManager)
    base = {
        "itemKey": "desk",
        "path": str(tmp_path / "desk.blend"),
        "sourceFingerprint": "a" * 64,
    }

    first = manager._normalize_items([{**base, "sourceDisplayName": "办公桌"}])
    second = manager._normalize_items([{**base, "sourceDisplayName": "会议桌"}])
    defaulted = manager._normalize_items([base])

    assert first[0]["sourceDisplayName"] == "办公桌"
    assert defaulted[0]["sourceDisplayName"] == "desk.blend"
    assert manager._request_fingerprint("PUBLISH", first) != manager._request_fingerprint(
        "PUBLISH", second
    )


def test_scene_asset_tool_schemas_are_strict_and_descriptive():
    async def list_tools():
        return await server.mcp.list_tools()

    tools = {tool.name: tool for tool in asyncio.run(list_tools())}

    discover = tools["discover_scene_asset_roots"]
    fingerprint_schema = discover.outputSchema["properties"]["scene_fingerprint"]
    string_fingerprint = next(branch for branch in fingerprint_schema["anyOf"] if branch.get("type") == "string")
    assert string_fingerprint["minLength"] == 64
    assert string_fingerprint["maxLength"] == 64
    root_schema = discover.outputSchema["$defs"]["SceneAssetRoot"]
    root_id_schema = root_schema["properties"]["root_object_id"]
    assert root_id_schema["minLength"] == root_id_schema["maxLength"] == 28

    start = tools["start_prepare_scene_assets"]
    item_ref = start.inputSchema["properties"]["items"]["items"]["$ref"]
    item_schema_name = item_ref.rsplit("/", 1)[-1]
    item_schema = start.inputSchema["$defs"][item_schema_name]
    assert item_schema["additionalProperties"] is False
    assert set(item_schema["required"]) >= {
        "asset_key",
        "root_object_ids",
    }
    assert item_schema["properties"]["root_object_ids"]["uniqueItems"] is True
    assert item_schema["properties"]["root_object_ids"]["minItems"] == 1
    assert item_schema["properties"]["root_object_ids"]["maxItems"] == 64
    assert item_schema["properties"]["root_object_ids"]["items"]["minLength"] == 28
    assert item_schema["properties"]["root_object_ids"]["items"]["pattern"] == "^obj_[0-9a-f]{24}$"
    assert item_schema["properties"]["asset_key"]["pattern"] == "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
    assert start.inputSchema["properties"]["items"]["maxItems"] == 64
    batch_id_schema = start.inputSchema["properties"]["batch_prepare_id"]["anyOf"][0]
    assert batch_id_schema["minLength"] == 1 and batch_id_schema["maxLength"] == 128
    assert "Omit for the initial scene batch" in batch_id_schema["description"]
    assert "response loss" in start.inputSchema["properties"]["idempotency_key"]["description"]

    prepare = tools["prepare_blend_asset"]
    source_schema = prepare.inputSchema["properties"]["source"]
    assert len(source_schema["oneOf"]) == 2
    assert all(variant["additionalProperties"] is False for variant in source_schema["oneOf"])
    assert source_schema["oneOf"][0]["properties"]["kind"]["const"] == "BLEND_FILE"
    assert source_schema["oneOf"][1]["properties"]["kind"]["const"] == "CURRENT_SELECTION"

    file_batch = tools["start_prepare_blend_assets"]
    file_item_ref = file_batch.inputSchema["properties"]["items"]["items"]["$ref"]
    file_item_schema = file_batch.inputSchema["$defs"][file_item_ref.rsplit("/", 1)[-1]]
    assert file_item_schema["additionalProperties"] is False
    assert set(file_item_schema["required"]) == {"itemKey", "path", "sourceFingerprint"}

    inspect = tools["inspect_prepared_assets"]
    inspect_item_ref = inspect.inputSchema["properties"]["items"]["items"]["$ref"]
    inspect_item_schema = inspect.inputSchema["$defs"][inspect_item_ref.rsplit("/", 1)[-1]]
    assert set(inspect_item_schema["required"]) == {"item_key", "prepare_id"}
    assert inspect.inputSchema["properties"]["items"]["maxItems"] == 16

    upload = tools["start_upload_prepared_artifacts"]
    upload_item_ref = upload.inputSchema["properties"]["items"]["items"]["$ref"]
    upload_item_schema = upload.inputSchema["$defs"][upload_item_ref.rsplit("/", 1)[-1]]
    assert upload_item_schema["additionalProperties"] is False
    assert {"itemKey", "artifactId", "method", "url"} <= set(upload_item_schema["required"])
    assert upload_item_schema["properties"]["method"]["const"] == "PUT"
    assert "AssetPlatform Upload Grant" in upload_item_schema["properties"]["url"]["description"]
    assert upload_item_schema["properties"]["url"]["pattern"] == "^https?://"
    assert upload_item_schema["properties"]["url"]["format"] == "uri"
    expected_sha_schema = upload_item_schema["properties"]["expectedSha256"]["anyOf"][0]
    assert expected_sha_schema["minLength"] == expected_sha_schema["maxLength"] == 64
    assert "response loss" in upload.inputSchema["properties"]["idempotency_key"]["description"]
    upload_id_schema = upload.inputSchema["properties"]["upload_id"]["anyOf"][0]
    assert upload_id_schema["minLength"] == 1 and upload_id_schema["maxLength"] == 128

    get_upload = tools["get_upload_prepared_artifacts"]
    assert get_upload.inputSchema["properties"]["upload_id"]["minLength"] == 1
    cursor_schema = get_upload.inputSchema["properties"]["cursor"]["anyOf"][0]
    assert cursor_schema["maxLength"] == 512
    cancel_upload = tools["cancel_upload_prepared_artifacts"]
    assert cancel_upload.inputSchema["properties"]["upload_id"]["maxLength"] == 128

    source_item_ref = start.outputSchema["properties"]["items"]["items"]["$ref"]
    source_item_schema = start.outputSchema["$defs"][source_item_ref.rsplit("/", 1)[-1]]
    assert {"itemKey", "sourceDisplayName", "sourceFingerprint"} <= set(source_item_schema["properties"])
    assert source_item_schema["properties"]["sourceFingerprint"]["minLength"] == 64
    assert start.outputSchema["properties"]["items"]["maxItems"] == 64

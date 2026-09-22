"""Prepared artifact capability security, integrity, TTL and lease tests."""

from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

import pytest

from blender_mcp.prepared_artifacts import PreparedArtifactError, PreparedArtifactStore


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_register_and_resolve_artifact_revalidates_exact_size_and_sha256(tmp_path: Path):
    clock = _Clock()
    workspace = tmp_path / "prepare-1"
    workspace.mkdir()
    payload = workspace / "payload.blend"
    payload_bytes = b"BLENDER-v300-publish-payload"
    payload.write_bytes(payload_bytes)

    store = PreparedArtifactStore(ttl_seconds=60, clock=clock)
    store.register_workspace("prepare-1", workspace)
    ref = store.register_artifact(
        "prepare-1",
        "PAYLOAD",
        payload,
        content_type="application/x-blender",
    )

    assert ref.kind == "PAYLOAD"
    assert ref.size == len(payload_bytes)
    assert ref.sha256 == _sha256_bytes(payload_bytes)
    assert ref.content_type == "application/x-blender"
    assert ref.expires_at.endswith("Z")
    assert not hasattr(ref, "path")
    assert store.resolve(ref.artifact_id) == payload.resolve()

    payload.write_bytes(payload_bytes + b"-tampered")
    with pytest.raises(PreparedArtifactError) as exc_info:
        store.resolve(ref.artifact_id)
    assert exc_info.value.code == "PREPARED_ARTIFACT_CHECKSUM_MISMATCH"


def test_random_expired_and_path_traversal_artifact_ids_are_rejected(tmp_path: Path):
    clock = _Clock()
    workspace = tmp_path / "prepare-2"
    workspace.mkdir()
    preview = workspace / "main.png"
    preview.write_bytes(b"\x89PNG\r\n\x1a\npreview")

    store = PreparedArtifactStore(ttl_seconds=5, clock=clock)
    store.register_workspace("prepare-2", workspace)
    ref = store.register_artifact(
        "prepare-2",
        "PREVIEW",
        preview,
        content_type="image/png",
    )

    for artifact_id in ("does-not-exist", "../../etc/passwd", "..\\..\\secret.txt"):
        with pytest.raises(PreparedArtifactError) as exc_info:
            store.resolve(artifact_id)
        assert exc_info.value.code == "PREPARED_ARTIFACT_NOT_FOUND"

    clock.advance(6)
    with pytest.raises(PreparedArtifactError) as exc_info:
        store.resolve(ref.artifact_id)
    assert exc_info.value.code == "PREPARED_ARTIFACT_EXPIRED"


def test_cleanup_expired_workspace_waits_for_active_lease(tmp_path: Path):
    clock = _Clock()
    workspace = tmp_path / "prepare-lease"
    workspace.mkdir()
    preview = workspace / "main.png"
    preview.write_bytes(b"preview")

    store = PreparedArtifactStore(ttl_seconds=5, clock=clock)
    store.register_workspace("prepare-lease", workspace)
    store.register_artifact(
        "prepare-lease",
        "PREVIEW",
        preview,
        content_type="image/png",
    )

    with store.lease("prepare-lease"):
        clock.advance(6)
        assert store.cleanup_expired() == []
        assert workspace.is_dir()

    assert store.cleanup_expired() == ["prepare-lease"]
    assert not workspace.exists()


def test_register_rejects_artifact_outside_prepare_workspace(tmp_path: Path):
    workspace = tmp_path / "prepare-safe"
    workspace.mkdir()
    outside = tmp_path / "outside.blend"
    outside.write_bytes(b"BLENDER-outside")

    store = PreparedArtifactStore()
    store.register_workspace("prepare-safe", workspace)

    with pytest.raises(PreparedArtifactError) as exc_info:
        store.register_artifact(
            "prepare-safe",
            "PAYLOAD",
            outside,
            content_type="application/x-blender",
        )
    assert exc_info.value.code == "PREPARED_ARTIFACT_INVALID_PATH"


def test_artifact_identity_hashes_exact_native_and_gzip_bytes(tmp_path: Path):
    workspace = tmp_path / "prepare-hashes"
    workspace.mkdir()
    native_bytes = b"BLENDER-v300-exact-byte-identity" * 32
    gzip_bytes = gzip.compress(native_bytes, mtime=0)
    native = workspace / "payload.blend"
    compressed = workspace / "payload.blend.gz"
    native.write_bytes(native_bytes)
    compressed.write_bytes(gzip_bytes)

    store = PreparedArtifactStore()
    store.register_workspace("prepare-hashes", workspace)
    native_ref = store.register_artifact(
        "prepare-hashes",
        "PAYLOAD",
        native,
        content_type="application/x-blender",
    )
    gzip_ref = store.register_artifact(
        "prepare-hashes",
        "PAYLOAD",
        compressed,
        content_type="application/gzip",
    )

    assert native_ref.size == len(native_bytes)
    assert native_ref.sha256 == _sha256_bytes(native_bytes)
    assert gzip_ref.size == len(gzip_bytes)
    assert gzip_ref.sha256 == _sha256_bytes(gzip_bytes)
    assert gzip_ref.sha256 != native_ref.sha256


def test_retained_evidence_identity_and_observation_cache_are_workspace_bound(tmp_path: Path):
    workspace = tmp_path / "prepare-observation"
    workspace.mkdir()
    retained = workspace / "source.blend"
    retained_bytes = b"BLENDER-retained-evidence"
    retained.write_bytes(retained_bytes)
    cache = workspace / "observation-cache.json"
    cache.write_text('{"prepareId":"prepare-observation"}', encoding="utf-8")

    store = PreparedArtifactStore(ttl_seconds=60)
    store.register_workspace(
        "prepare-observation",
        workspace,
        retained_source_path=retained,
        retained_source_kind="SOURCE_SNAPSHOT",
        observation_cache_path=cache,
    )

    assert store.evidence_identity("prepare-observation") == {
        "prepareId": "prepare-observation",
        "kind": "SOURCE_SNAPSHOT",
        "size": len(retained_bytes),
        "sha256": _sha256_bytes(retained_bytes),
    }
    assert store.observation_cache_path("prepare-observation") == cache.resolve()

    retained.write_bytes(retained_bytes + b"-tampered")
    with pytest.raises(PreparedArtifactError) as exc_info:
        store.evidence_identity("prepare-observation")
    assert exc_info.value.code == "PREPARED_ARTIFACT_CHECKSUM_MISMATCH"


def test_register_workspace_rejects_observation_cache_outside_workspace(tmp_path: Path):
    workspace = tmp_path / "prepare-cache-safe"
    workspace.mkdir()
    retained = workspace / "source.blend"
    retained.write_bytes(b"BLENDER-source")
    outside_cache = tmp_path / "outside-observation.json"
    outside_cache.write_text("{}", encoding="utf-8")

    store = PreparedArtifactStore()
    with pytest.raises(PreparedArtifactError) as exc_info:
        store.register_workspace(
            "prepare-cache-safe",
            workspace,
            retained_source_path=retained,
            retained_source_kind="SOURCE_SNAPSHOT",
            observation_cache_path=outside_cache,
        )
    assert exc_info.value.code == "PREPARED_ARTIFACT_INVALID_PATH"


def test_renew_prepare_extends_live_workspace_and_artifacts_without_reviving_expired(tmp_path: Path):
    clock = _Clock()
    workspace = tmp_path / "prepare-renew"
    workspace.mkdir()
    preview = workspace / "main.png"
    preview.write_bytes(b"preview-renew")

    store = PreparedArtifactStore(ttl_seconds=5, clock=clock)
    store.register_workspace("prepare-renew", workspace)
    original = store.register_artifact(
        "prepare-renew",
        "PREVIEW",
        preview,
        content_type="image/png",
    )

    clock.advance(4)
    renewed = store.renew_prepare("prepare-renew")
    assert renewed["prepareId"] == "prepare-renew"
    assert renewed["expiresAt"] > original.expires_at
    assert store.artifact_ref(original.artifact_id).expires_at == renewed["expiresAt"]

    clock.advance(2)
    assert store.resolve(original.artifact_id) == preview.resolve()

    clock.advance(4)
    with pytest.raises(PreparedArtifactError) as exc_info:
        store.renew_prepare("prepare-renew")
    assert exc_info.value.code == "PREPARED_ARTIFACT_EXPIRED"


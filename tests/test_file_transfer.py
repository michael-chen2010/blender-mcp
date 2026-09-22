"""Prepared artifact signed-URL upload and batch job tests."""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from blender_mcp.file_transfer import FileTransferError, PreparedArtifactTransferService
from blender_mcp.prepared_artifacts import PreparedArtifactStore


class _Clock:
    def __init__(self, value: float = 1_800_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _UploadFixture:
    def __init__(self):
        self.lock = threading.Lock()
        self.received: dict[str, list[bytes]] = defaultdict(list)
        self.statuses: dict[str, list[int]] = defaultdict(list)
        self.active = 0
        self.max_active = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.block_paths: set[str] = set()
        self.delay_seconds: dict[str, float] = {}

    def queue_status(self, path: str, *statuses: int) -> None:
        self.statuses[path].extend(statuses)

    def next_status(self, path: str) -> int:
        with self.lock:
            queue = self.statuses[path]
            if queue:
                return queue.pop(0)
        return 200

    def request_count(self, path: str) -> int:
        with self.lock:
            return len(self.received[path])


@contextmanager
def _upload_server(fixture: _UploadFixture):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_PUT(self):  # noqa: N802 - stdlib handler API
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            with fixture.lock:
                fixture.received[path].append(body)
                fixture.active += 1
                fixture.max_active = max(fixture.max_active, fixture.active)
            fixture.started.set()
            try:
                if path in fixture.block_paths:
                    fixture.release.wait(timeout=5)
                delay = fixture.delay_seconds.get(path, 0.0)
                if delay:
                    time.sleep(delay)
                status = fixture.next_status(path)
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with fixture.lock:
                    fixture.active -= 1

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        fixture.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _register_payloads(
    tmp_path: Path,
    *,
    prepare_id: str = "prepare-upload",
    count: int = 1,
    ttl_seconds: float = 60,
    clock=None,
    payload_size: int = 256 * 1024 + 37,
):
    workspace = tmp_path / prepare_id
    workspace.mkdir()
    store = PreparedArtifactStore(ttl_seconds=ttl_seconds, clock=clock or time.time)
    store.register_workspace(prepare_id, workspace)
    refs = []
    paths = []
    for index in range(count):
        path = workspace / f"payload-{index}.blend"
        seed = f"BLENDER-upload-{index}-".encode("ascii")
        repeats = payload_size // len(seed) + 1
        data = (seed * repeats)[:payload_size]
        path.write_bytes(data)
        ref = store.register_artifact(
            prepare_id,
            "PAYLOAD",
            path,
            content_type="application/x-blender",
        )
        refs.append(ref)
        paths.append(path)
    return store, refs, paths


def _grant_item(item_key, ref, url: str, headers: dict[str, str] | None = None):
    return {
        "itemKey": item_key,
        "artifactId": ref.artifact_id,
        "method": "PUT",
        "url": url,
        "headers": dict(headers or {}),
        "expectedSize": ref.size,
        "expectedSha256": ref.sha256,
    }


async def _wait_terminal(service: PreparedArtifactTransferService, upload_id: str, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page = service.get_upload_prepared_artifacts(upload_id)
        if page["status"] in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}:
            return page
        await asyncio.sleep(0.01)
    raise AssertionError(f"upload job did not finish: {service.get_upload_prepared_artifacts(upload_id)}")


def test_single_upload_streams_exact_registered_bytes_and_reports_timings(tmp_path: Path):
    store, refs, paths = _register_payloads(tmp_path, payload_size=1024 * 1024 + 123)
    fixture = _UploadFixture()

    async def scenario(base_url: str):
        service = PreparedArtifactTransferService(store, chunk_size=64 * 1024)
        return await service.upload_prepared_artifact(
            refs[0].artifact_id,
            "PUT",
            f"{base_url}/payload?X-Amz-Signature=secret-signature",
            {"Authorization": "Bearer secret-token", "Content-Type": "application/x-blender"},
            expected_size=refs[0].size,
            expected_sha256=refs[0].sha256,
        )

    with _upload_server(fixture) as base_url:
        result = asyncio.run(scenario(base_url))

    expected = paths[0].read_bytes()
    assert fixture.received["/payload"] == [expected]
    assert result["artifactId"] == refs[0].artifact_id
    assert result["status"] == "SUCCEEDED"
    assert result["timings"]["bytes"] == len(expected)
    assert result["timings"]["httpStatus"] == 200
    assert result["timings"]["hashVerifyMs"] >= 0
    assert result["timings"]["putMs"] >= 0
    assert result["timings"]["queueMs"] >= 0
    assert result["timings"]["throughputMiBps"] >= 0


def test_upload_accepts_only_registered_handles_and_rejects_unknown_or_expired(tmp_path: Path):
    clock = _Clock()
    store, refs, paths = _register_payloads(tmp_path, ttl_seconds=5, clock=clock)
    fixture = _UploadFixture()

    async def expect_code(service, artifact_id, url, code):
        with pytest.raises(FileTransferError) as exc_info:
            await service.upload_prepared_artifact(artifact_id, "PUT", url)
        assert exc_info.value.code == code

    with _upload_server(fixture) as base_url:
        service = PreparedArtifactTransferService(store)
        asyncio.run(expect_code(service, str(paths[0]), f"{base_url}/path", "PREPARED_ARTIFACT_NOT_FOUND"))
        asyncio.run(expect_code(service, "not-a-real-handle", f"{base_url}/unknown", "PREPARED_ARTIFACT_NOT_FOUND"))
        clock.advance(6)
        asyncio.run(expect_code(service, refs[0].artifact_id, f"{base_url}/expired-handle", "PREPARED_ARTIFACT_EXPIRED"))

    assert fixture.request_count("/path") == 0
    assert fixture.request_count("/unknown") == 0
    assert fixture.request_count("/expired-handle") == 0


def test_upload_rejects_grant_identity_mismatch_before_http(tmp_path: Path):
    store, refs, _ = _register_payloads(tmp_path)
    fixture = _UploadFixture()

    async def scenario(base_url: str):
        service = PreparedArtifactTransferService(store)
        with pytest.raises(FileTransferError) as size_exc:
            await service.upload_prepared_artifact(
                refs[0].artifact_id,
                "PUT",
                f"{base_url}/wrong-size",
                expected_size=refs[0].size + 1,
            )
        assert size_exc.value.code == "UPLOAD_EXPECTED_SIZE_MISMATCH"

        with pytest.raises(FileTransferError) as sha_exc:
            await service.upload_prepared_artifact(
                refs[0].artifact_id,
                "PUT",
                f"{base_url}/wrong-sha",
                expected_sha256="0" * 64,
            )
        assert sha_exc.value.code == "UPLOAD_EXPECTED_SHA256_MISMATCH"

    with _upload_server(fixture) as base_url:
        asyncio.run(scenario(base_url))

    assert fixture.request_count("/wrong-size") == 0
    assert fixture.request_count("/wrong-sha") == 0


def test_preupload_checksum_recheck_blocks_tampered_bytes_before_http(tmp_path: Path):
    store, refs, paths = _register_payloads(tmp_path)
    original = paths[0].read_bytes()
    paths[0].write_bytes(original + b"tampered")
    fixture = _UploadFixture()

    async def scenario(base_url: str):
        service = PreparedArtifactTransferService(store)
        with pytest.raises(FileTransferError) as exc_info:
            await service.upload_prepared_artifact(refs[0].artifact_id, "PUT", f"{base_url}/must-not-run")
        assert exc_info.value.code == "PREPARED_ARTIFACT_CHECKSUM_MISMATCH"

    with _upload_server(fixture) as base_url:
        asyncio.run(scenario(base_url))

    assert fixture.request_count("/must-not-run") == 0


def test_http_failure_and_expired_url_are_stable_and_secret_redacted(tmp_path: Path, caplog):
    store, refs, _ = _register_payloads(tmp_path)
    fixture = _UploadFixture()
    fixture.queue_status("/server-error", 500)
    fixture.queue_status("/expired-url", 403)
    signed_secret = "top-secret-signature"
    auth_secret = "Bearer top-secret-authorization"

    async def scenario(base_url: str):
        service = PreparedArtifactTransferService(store)
        with pytest.raises(FileTransferError) as server_exc:
            await service.upload_prepared_artifact(
                refs[0].artifact_id,
                "PUT",
                f"{base_url}/server-error?X-Amz-Signature={signed_secret}",
                {"Authorization": auth_secret},
            )
        assert server_exc.value.code == "UPLOAD_HTTP_ERROR"
        assert signed_secret not in str(server_exc.value)
        assert auth_secret not in str(server_exc.value)

        with pytest.raises(FileTransferError) as expiry_exc:
            await service.upload_prepared_artifact(
                refs[0].artifact_id,
                "PUT",
                f"{base_url}/expired-url?X-Amz-Signature={signed_secret}",
                {"Authorization": auth_secret},
            )
        assert expiry_exc.value.code == "SIGNED_URL_EXPIRED"
        assert signed_secret not in str(expiry_exc.value)
        assert auth_secret not in str(expiry_exc.value)

    with _upload_server(fixture) as base_url:
        asyncio.run(scenario(base_url))

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert signed_secret not in logs
    assert auth_secret not in logs
    assert "X-Amz-Signature" not in logs


def test_unexpected_transport_failure_is_sanitized(tmp_path: Path, monkeypatch, caplog):
    from blender_mcp import file_transfer as transfer_module

    store, refs, _ = _register_payloads(tmp_path)
    signed_secret = "unexpected-secret-signature"
    auth_secret = "Bearer unexpected-secret-authorization"

    class ExplodingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, *, headers, content):
            raise RuntimeError(f"transport exploded for {url} with {headers.get('Authorization')}")

    monkeypatch.setattr(transfer_module.httpx, "AsyncClient", ExplodingClient)

    async def scenario():
        service = PreparedArtifactTransferService(store)
        with pytest.raises(FileTransferError) as exc_info:
            await service.upload_prepared_artifact(
                refs[0].artifact_id,
                "PUT",
                f"https://uploads.example.test/object?X-Amz-Signature={signed_secret}",
                {"Authorization": auth_secret},
            )
        assert exc_info.value.code == "UPLOAD_HTTP_ERROR"
        assert signed_secret not in str(exc_info.value)
        assert auth_secret not in str(exc_info.value)

    asyncio.run(scenario())
    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert signed_secret not in logs
    assert auth_secret not in logs


def test_batch_rejects_duplicate_artifact_id_even_when_item_keys_differ(tmp_path: Path):
    store, refs, _ = _register_payloads(tmp_path, count=1)
    service = PreparedArtifactTransferService(store)
    first = _grant_item("item-a", refs[0], "https://uploads.example.test/first")
    second = _grant_item("item-b", refs[0], "https://uploads.example.test/second")

    async def scenario():
        with pytest.raises(FileTransferError) as duplicate:
            await service.start_upload_prepared_artifacts(
                "batch-prepare-1",
                [first, second],
                "duplicate-artifact",
            )
        assert duplicate.value.code == "UPLOAD_DUPLICATE_ARTIFACT"

    asyncio.run(scenario())


def test_batch_allows_multiple_artifacts_for_the_same_item_key(tmp_path: Path):
    store, refs, paths = _register_payloads(tmp_path, count=2)
    fixture = _UploadFixture()

    async def scenario(base_url: str):
        manifest = {
            "item-a": {refs[0].artifact_id, refs[1].artifact_id},
        }
        service = PreparedArtifactTransferService(
            store,
            prepare_manifest_resolver=lambda batch_prepare_id: manifest
            if batch_prepare_id == "batch-prepare-1"
            else None,
        )
        started = await service.start_upload_prepared_artifacts(
            "batch-prepare-1",
            [
                _grant_item("item-a", refs[0], f"{base_url}/payload"),
                _grant_item("item-a", refs[1], f"{base_url}/main-preview"),
            ],
            "same-item-multiple-artifacts",
            concurrency=2,
        )
        terminal = await _wait_terminal(service, started["uploadId"])

        assert terminal["status"] == "SUCCEEDED"
        assert len(terminal["items"]) == 2
        assert [item["itemKey"] for item in terminal["items"]] == ["item-a", "item-a"]
        assert {item["artifactId"] for item in terminal["items"]} == {
            refs[0].artifact_id,
            refs[1].artifact_id,
        }
        assert fixture.received["/payload"] == [paths[0].read_bytes()]
        assert fixture.received["/main-preview"] == [paths[1].read_bytes()]

    with _upload_server(fixture) as base_url:
        asyncio.run(scenario(base_url))


def test_batch_partial_failure_retry_membership_and_success_replay(tmp_path: Path):
    store, refs, _ = _register_payloads(tmp_path, count=3)
    fixture = _UploadFixture()
    fixture.queue_status("/ok", 200)
    fixture.queue_status("/retry", 500)
    fixture.queue_status("/retry-renewed", 200)

    async def scenario(base_url: str):
        service = PreparedArtifactTransferService(store, page_size=1)
        ok_url = f"{base_url}/ok?X-Amz-Signature=batch-ok-secret"
        retry_url = f"{base_url}/retry?X-Amz-Signature=batch-retry-secret"
        renewed_url = f"{base_url}/retry-renewed?X-Amz-Signature=batch-renewed-secret"
        initial_items = [
            _grant_item("item-a", refs[0], ok_url),
            _grant_item("item-b", refs[1], retry_url),
        ]
        initial = await service.start_upload_prepared_artifacts(
            "batch-prepare-1",
            initial_items,
            "request-1",
            concurrency=2,
        )
        upload_id = initial["uploadId"]
        first_page = await _wait_terminal(service, upload_id)
        assert first_page["status"] == "PARTIAL"
        assert first_page["nextCursor"] == "1"
        assert first_page["items"][0]["itemKey"] == "item-a"
        second_page = service.get_upload_prepared_artifacts(upload_id, first_page["nextCursor"])
        assert second_page["nextCursor"] is None
        assert second_page["items"][0]["itemKey"] == "item-b"
        retained_job = repr(service._jobs[upload_id])
        assert "batch-ok-secret" not in retained_job
        assert "batch-retry-secret" not in retained_job

        replay = await service.start_upload_prepared_artifacts(
            "batch-prepare-1",
            initial_items,
            "request-1",
            upload_id=upload_id,
        )
        assert replay["uploadId"] == upload_id
        await asyncio.sleep(0.05)
        assert fixture.request_count("/ok") == 1
        assert fixture.request_count("/retry") == 1

        with pytest.raises(FileTransferError) as conflict:
            await service.start_upload_prepared_artifacts(
                "batch-prepare-1",
                [_grant_item("item-b", refs[1], renewed_url)],
                "request-1",
                upload_id=upload_id,
            )
        assert conflict.value.code == "UPLOAD_IDEMPOTENCY_CONFLICT"

        with pytest.raises(FileTransferError) as membership:
            await service.start_upload_prepared_artifacts(
                "batch-prepare-1",
                [_grant_item("item-c", refs[2], f"{base_url}/outside")],
                "request-outside",
                upload_id=upload_id,
            )
        assert membership.value.code == "UPLOAD_ARTIFACT_NOT_IN_JOB"

        with pytest.raises(FileTransferError) as wrong_item:
            await service.start_upload_prepared_artifacts(
                "batch-prepare-1",
                [_grant_item("item-renamed", refs[1], renewed_url)],
                "request-renamed",
                upload_id=upload_id,
            )
        assert wrong_item.value.code == "UPLOAD_ARTIFACT_NOT_IN_JOB"

        retry = await service.start_upload_prepared_artifacts(
            "batch-prepare-1",
            [_grant_item("item-b", refs[1], renewed_url)],
            "request-2",
            upload_id=upload_id,
        )
        assert retry["uploadId"] == upload_id
        terminal = await _wait_terminal(service, upload_id)
        assert terminal["status"] == "SUCCEEDED"
        assert fixture.request_count("/retry") == 1
        assert fixture.request_count("/retry-renewed") == 1

        assert "batch-renewed-secret" not in repr(service._jobs[upload_id])

        await service.start_upload_prepared_artifacts(
            "batch-prepare-1",
            [_grant_item("item-a", refs[0], ok_url)],
            "request-3",
            upload_id=upload_id,
        )
        await asyncio.sleep(0.05)
        assert fixture.request_count("/ok") == 1

    with _upload_server(fixture) as base_url:
        asyncio.run(scenario(base_url))


def test_batch_concurrency_limit_is_enforced(tmp_path: Path):
    store, refs, _ = _register_payloads(tmp_path, count=4, payload_size=64 * 1024)
    fixture = _UploadFixture()
    for index in range(4):
        fixture.delay_seconds[f"/item-{index}"] = 0.08

    async def scenario(base_url: str):
        service = PreparedArtifactTransferService(store)
        started = await service.start_upload_prepared_artifacts(
            "batch-concurrency",
            [
                _grant_item(f"item-{index}", ref, f"{base_url}/item-{index}")
                for index, ref in enumerate(refs)
            ],
            "request-concurrency",
            concurrency=2,
        )
        terminal = await _wait_terminal(service, started["uploadId"])
        assert terminal["status"] == "SUCCEEDED"

    with _upload_server(fixture) as base_url:
        asyncio.run(scenario(base_url))

    assert fixture.max_active == 2


def test_cancelled_batch_releases_lease_and_ttl_cleanup_waits_for_active_upload(tmp_path: Path):
    clock = _Clock()
    store, refs, _ = _register_payloads(tmp_path, ttl_seconds=5, clock=clock, payload_size=64 * 1024)
    workspace = tmp_path / "prepare-upload"
    fixture = _UploadFixture()
    fixture.block_paths.add("/blocked")

    async def scenario(base_url: str):
        service = PreparedArtifactTransferService(store)
        started = await service.start_upload_prepared_artifacts(
            "batch-cancel",
            [_grant_item("item-blocked", refs[0], f"{base_url}/blocked?X-Amz-Signature=cancel-secret")],
            "request-cancel",
        )
        upload_id = started["uploadId"]
        assert await asyncio.to_thread(fixture.started.wait, 1.0)
        clock.advance(6)
        assert store.cleanup_expired() == []
        assert workspace.is_dir()

        cancelled = await service.cancel_upload_prepared_artifacts(upload_id)
        assert cancelled["uploadId"] == upload_id
        terminal = await _wait_terminal(service, upload_id)
        assert terminal["status"] == "CANCELLED"
        assert "cancel-secret" not in repr(service._jobs[upload_id])
        fixture.release.set()
        assert store.cleanup_expired() == ["prepare-upload"]
        assert not workspace.exists()

    with _upload_server(fixture) as base_url:
        asyncio.run(scenario(base_url))


def test_server_transfer_tools_return_stable_structured_errors():
    from blender_mcp import server

    result = asyncio.run(
        server.upload_prepared_artifact(
            artifact_id="missing-artifact",
            method="PUT",
            signed_url="http://127.0.0.1:1/never-used?token=secret",
        )
    )
    assert result.isError is True
    assert result.structuredContent["error"]["code"] == "PREPARED_ARTIFACT_NOT_FOUND"
    assert "token=secret" not in result.content[0].text

    batch_result = asyncio.run(
        server.start_upload_prepared_artifacts(
            batch_prepare_id="batch-empty",
            items=[],
            idempotency_key="request-empty",
        )
    )
    assert batch_result.isError is True
    assert batch_result.structuredContent["error"]["code"] == "UPLOAD_ITEMS_REQUIRED"

"""Signed-URL transfer for short-lived prepared artifact capabilities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import secrets
import threading
import time
from typing import Any, AsyncIterator, Callable, Mapping
from urllib.parse import urlsplit

import httpx

from .prepared_artifacts import PreparedArtifactError, PreparedArtifactStore

logger = logging.getLogger("blender-mcp-file-transfer")

_DEFAULT_CHUNK_SIZE = 1024 * 1024
_DEFAULT_BATCH_CONCURRENCY = 4
_MAX_BATCH_CONCURRENCY = 12
_DEFAULT_PAGE_SIZE = 50
_TERMINAL_ITEM_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}
_TERMINAL_JOB_STATES = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}


class FileTransferError(RuntimeError):
    """Stable transfer error whose text never contains signed URL credentials."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass
class _UploadItem:
    item_key: str
    artifact_id: str
    method: str
    signed_url: str
    headers: dict[str, str]
    expected_size: int | None
    expected_sha256: str | None
    status: str = "PENDING"
    attempts: int = 0
    queued_at: float = 0.0
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None


@dataclass
class _UploadJob:
    upload_id: str
    batch_prepare_id: str
    order: list[str]
    members: set[str]
    items: dict[str, _UploadItem]
    concurrency: int
    status: str = "RUNNING"
    cancel_requested: bool = False
    request_fingerprints: dict[str, str] = field(default_factory=dict)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


def _prepared_error(exc: PreparedArtifactError) -> FileTransferError:
    message = str(exc)
    _, separator, detail = message.partition(":")
    return FileTransferError(exc.code, detail.strip() if separator else "Prepared artifact is unavailable")


def _request_fingerprint(batch_prepare_id: str, items: list[dict[str, Any]]) -> str:
    normalized = {
        "batchPrepareId": batch_prepare_id,
        "items": [
            {
                "itemKey": item["itemKey"],
                "artifactId": item["artifactId"],
                "method": item["method"],
                "url": item["url"],
                "headers": sorted(item["headers"].items()),
                "expectedSize": item["expectedSize"],
                "expectedSha256": item["expectedSha256"],
            }
            for item in items
        ],
    }
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise FileTransferError("UPLOAD_HEADERS_INVALID", "headers must be an object of string values")
    normalized: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise FileTransferError("UPLOAD_HEADERS_INVALID", "headers must be an object of string values")
        normalized[key] = value
    return normalized


def _normalize_method(method: Any) -> str:
    if not isinstance(method, str) or not method.strip():
        raise FileTransferError("UPLOAD_METHOD_INVALID", "method is required")
    normalized = method.strip().upper()
    if normalized != "PUT":
        raise FileTransferError("UPLOAD_METHOD_UNSUPPORTED", "only PUT is supported for prepared artifact upload")
    return normalized


def _normalize_url(signed_url: Any) -> str:
    if not isinstance(signed_url, str) or not signed_url.strip():
        raise FileTransferError("SIGNED_URL_INVALID", "signed_url is required")
    value = signed_url.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise FileTransferError("SIGNED_URL_INVALID", "signed_url must be an absolute HTTP(S) URL")
    return value


def _normalize_expected_size(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FileTransferError("UPLOAD_EXPECTED_SIZE_INVALID", "expectedSize must be a non-negative integer")
    return value


def _normalize_expected_sha256(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64:
        raise FileTransferError("UPLOAD_EXPECTED_SHA256_INVALID", "expectedSha256 must be a 64-character hex digest")
    normalized = value.lower()
    if any(char not in "0123456789abcdef" for char in normalized):
        raise FileTransferError("UPLOAD_EXPECTED_SHA256_INVALID", "expectedSha256 must be a 64-character hex digest")
    return normalized


def _normalize_item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise FileTransferError("UPLOAD_ITEM_INVALID", "each upload item must be an object")
    item_key = raw.get("itemKey")
    if not isinstance(item_key, str) or not item_key:
        raise FileTransferError("UPLOAD_ITEM_KEY_REQUIRED", "itemKey is required")
    artifact_id = raw.get("artifactId")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise FileTransferError("UPLOAD_ARTIFACT_ID_REQUIRED", "artifactId is required")
    raw_url = raw.get("url")
    legacy_url = raw.get("signedUrl")
    if raw_url is not None and legacy_url is not None and raw_url != legacy_url:
        raise FileTransferError("SIGNED_URL_INVALID", "url and signedUrl cannot disagree")
    return {
        "itemKey": item_key,
        "artifactId": artifact_id,
        "method": _normalize_method(raw.get("method")),
        "url": _normalize_url(raw_url if raw_url is not None else legacy_url),
        "headers": _normalize_headers(raw.get("headers")),
        "expectedSize": _normalize_expected_size(raw.get("expectedSize")),
        "expectedSha256": _normalize_expected_sha256(raw.get("expectedSha256")),
    }


async def _iter_file(path: Path, chunk_size: int, counter: list[int]) -> AsyncIterator[bytes]:
    handle = await asyncio.to_thread(path.open, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(handle.read, chunk_size)
            if not chunk:
                break
            counter[0] += len(chunk)
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


class PreparedArtifactTransferService:
    """Uploads prepared artifact handles without exposing or accepting local paths."""

    def __init__(
        self,
        artifact_store: PreparedArtifactStore,
        *,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        page_size: int = _DEFAULT_PAGE_SIZE,
        prepare_manifest_resolver: Callable[[str], Mapping[str, set[str]] | None] | None = None,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self._artifact_store = artifact_store
        self._chunk_size = int(chunk_size)
        self._page_size = int(page_size)
        self._prepare_manifest_resolver = prepare_manifest_resolver
        self._jobs: dict[str, _UploadJob] = {}
        self._lock = threading.RLock()

    def _artifact_prepare_id(self, artifact_id: str) -> str:
        try:
            return self._artifact_store.artifact_prepare_id(artifact_id)
        except PreparedArtifactError as exc:
            raise _prepared_error(exc) from exc

    def _artifact_ref(self, artifact_id: str):
        try:
            return self._artifact_store.artifact_ref(artifact_id)
        except PreparedArtifactError as exc:
            raise _prepared_error(exc) from exc

    async def _upload(
        self,
        artifact_id: str,
        method: str,
        signed_url: str,
        headers: Mapping[str, Any] | None,
        *,
        queued_at: float,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        normalized_method = _normalize_method(method)
        normalized_url = _normalize_url(signed_url)
        normalized_headers = _normalize_headers(headers)
        normalized_expected_size = _normalize_expected_size(expected_size)
        normalized_expected_sha256 = _normalize_expected_sha256(expected_sha256)
        prepare_id = self._artifact_prepare_id(artifact_id)
        queue_ms = max(0.0, (time.perf_counter() - queued_at) * 1000.0)

        try:
            with self._artifact_store.lease(prepare_id):
                verify_started = time.perf_counter()
                artifact_ref = self._artifact_ref(artifact_id)
                if normalized_expected_size is not None and normalized_expected_size != artifact_ref.size:
                    raise FileTransferError(
                        "UPLOAD_EXPECTED_SIZE_MISMATCH",
                        "Upload grant expectedSize does not match the prepared artifact identity",
                    )
                if normalized_expected_sha256 is not None and normalized_expected_sha256 != artifact_ref.sha256:
                    raise FileTransferError(
                        "UPLOAD_EXPECTED_SHA256_MISMATCH",
                        "Upload grant expectedSha256 does not match the prepared artifact identity",
                    )
                try:
                    path = await asyncio.to_thread(self._artifact_store.resolve, artifact_id)
                except PreparedArtifactError as exc:
                    raise _prepared_error(exc) from exc
                size = path.stat().st_size
                hash_verify_ms = max(0.0, (time.perf_counter() - verify_started) * 1000.0)

                request_headers = dict(normalized_headers)
                supplied_length = next(
                    (value for key, value in request_headers.items() if key.lower() == "content-length"),
                    None,
                )
                if supplied_length is not None:
                    try:
                        if int(supplied_length) != size:
                            raise ValueError
                    except ValueError as exc:
                        raise FileTransferError(
                            "UPLOAD_CONTENT_LENGTH_MISMATCH",
                            "Content-Length does not match the prepared artifact size",
                        ) from exc
                else:
                    request_headers["Content-Length"] = str(size)

                sent = [0]
                put_started = time.perf_counter()
                status = 0
                try:
                    async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
                        async with client.stream(
                            normalized_method,
                            normalized_url,
                            headers=request_headers,
                            content=_iter_file(path, self._chunk_size, sent),
                        ) as response:
                            status = response.status_code
                except asyncio.CancelledError:
                    raise
                except httpx.HTTPError as exc:
                    raise FileTransferError("UPLOAD_HTTP_ERROR", "HTTP upload failed before a response was accepted") from exc
                except Exception as exc:
                    raise FileTransferError("UPLOAD_HTTP_ERROR", "HTTP upload failed before a response was accepted") from exc

                put_seconds = max(0.0, time.perf_counter() - put_started)
                if status in {401, 403}:
                    raise FileTransferError("SIGNED_URL_EXPIRED", f"Signed upload URL was rejected with HTTP {status}")
                if status < 200 or status >= 300:
                    raise FileTransferError("UPLOAD_HTTP_ERROR", f"Upload endpoint returned HTTP {status}")
                if sent[0] != size:
                    raise FileTransferError(
                        "UPLOAD_BYTE_COUNT_MISMATCH",
                        "Uploaded byte count does not match the prepared artifact size",
                    )

                throughput = 0.0
                if put_seconds > 0:
                    throughput = (sent[0] / (1024 * 1024)) / put_seconds
                return {
                    "artifactId": artifact_id,
                    "status": "SUCCEEDED",
                    "timings": {
                        "queueMs": round(queue_ms, 3),
                        "hashVerifyMs": round(hash_verify_ms, 3),
                        "putMs": round(put_seconds * 1000.0, 3),
                        "bytes": sent[0],
                        "throughputMiBps": round(throughput, 3),
                        "httpStatus": status,
                    },
                }
        except PreparedArtifactError as exc:
            raise _prepared_error(exc) from exc

    async def upload_prepared_artifact(
        self,
        artifact_id: str,
        method: str,
        signed_url: str,
        headers: Mapping[str, Any] | None = None,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(artifact_id, str) or not artifact_id:
            raise FileTransferError("UPLOAD_ARTIFACT_ID_REQUIRED", "artifact_id is required")
        queued_at = time.perf_counter()
        return await self._upload(
            artifact_id,
            method,
            signed_url,
            headers,
            queued_at=queued_at,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    def _validate_artifact_handle(self, artifact_id: str) -> None:
        self._artifact_prepare_id(artifact_id)

    def _validate_batch_prepare_membership(
        self,
        batch_prepare_id: str,
        items: list[dict[str, Any]],
    ) -> None:
        if self._prepare_manifest_resolver is None:
            return
        manifest = self._prepare_manifest_resolver(batch_prepare_id)
        if manifest is None:
            raise FileTransferError(
                "UPLOAD_BATCH_PREPARE_NOT_FOUND",
                "Unknown batch_prepare_id for prepared artifact upload",
            )
        for item in items:
            artifact_ids = manifest.get(item["itemKey"])
            if artifact_ids is None or item["artifactId"] not in artifact_ids:
                raise FileTransferError(
                    "UPLOAD_ARTIFACT_NOT_IN_BATCH_PREPARE",
                    "upload item/artifact does not belong to the batch prepare manifest",
                )

    def _normalize_concurrency(self, concurrency: int | None) -> int:
        if concurrency is None:
            return _DEFAULT_BATCH_CONCURRENCY
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency <= 0:
            raise FileTransferError("UPLOAD_CONCURRENCY_INVALID", "concurrency must be a positive integer")
        return min(concurrency, _MAX_BATCH_CONCURRENCY)

    def _new_upload_id(self) -> str:
        upload_id = secrets.token_urlsafe(18)
        with self._lock:
            while upload_id in self._jobs:
                upload_id = secrets.token_urlsafe(18)
        return upload_id

    def _recompute_job_status(self, job: _UploadJob) -> None:
        statuses = [job.items[artifact_id].status for artifact_id in job.order]
        if any(status not in _TERMINAL_ITEM_STATES for status in statuses):
            job.status = "RUNNING"
            return
        if statuses and all(status == "SUCCEEDED" for status in statuses):
            job.status = "SUCCEEDED"
            return
        if statuses and all(status == "CANCELLED" for status in statuses):
            job.status = "CANCELLED"
            return
        if any(status == "SUCCEEDED" for status in statuses):
            job.status = "PARTIAL"
            return
        if any(status == "FAILED" for status in statuses):
            job.status = "FAILED"
            return
        job.status = "CANCELLED"

    async def _run_job_items(
        self,
        job: _UploadJob,
        artifact_ids: list[str],
        concurrency: int,
    ) -> None:
        semaphore = asyncio.Semaphore(concurrency)

        async def run_one(artifact_id: str) -> None:
            item = job.items[artifact_id]
            try:
                async with semaphore:
                    with self._lock:
                        if job.cancel_requested:
                            item.status = "CANCELLED"
                            self._recompute_job_status(job)
                            return
                        item.status = "RUNNING"
                        item.attempts += 1
                        queued_at = item.queued_at
                        self._recompute_job_status(job)
                    try:
                        result = await self._upload(
                            item.artifact_id,
                            item.method,
                            item.signed_url,
                            item.headers,
                            queued_at=queued_at,
                            expected_size=item.expected_size,
                            expected_sha256=item.expected_sha256,
                        )
                    except asyncio.CancelledError:
                        with self._lock:
                            item.status = "CANCELLED"
                            item.error = {"code": "UPLOAD_CANCELLED", "message": "Upload was cancelled"}
                            self._recompute_job_status(job)
                        raise
                    except FileTransferError as exc:
                        with self._lock:
                            item.status = "FAILED"
                            item.error = {"code": exc.code, "message": exc.message}
                            item.result = None
                            self._recompute_job_status(job)
                    else:
                        with self._lock:
                            item.status = "SUCCEEDED"
                            item.result = result
                            item.error = None
                            self._recompute_job_status(job)
            finally:
                with self._lock:
                    item.signed_url = ""
                    item.headers.clear()
                    self._recompute_job_status(job)

        tasks = [asyncio.create_task(run_one(artifact_id)) for artifact_id in artifact_ids]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            with self._lock:
                self._recompute_job_status(job)

    def _track_job_task(self, job: _UploadJob, task: asyncio.Task[Any]) -> None:
        with self._lock:
            job.tasks.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            with self._lock:
                job.tasks.discard(completed)
                self._recompute_job_status(job)
            try:
                completed.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.error(
                    "Prepared artifact upload job task failed with an internal error",
                    extra={"upload_id": job.upload_id},
                )

        task.add_done_callback(done)

    async def start_upload_prepared_artifacts(
        self,
        batch_prepare_id: str,
        items: list[Mapping[str, Any]],
        idempotency_key: str,
        *,
        upload_id: str | None = None,
        concurrency: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(batch_prepare_id, str) or not batch_prepare_id:
            raise FileTransferError("BATCH_PREPARE_ID_REQUIRED", "batch_prepare_id is required")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise FileTransferError("UPLOAD_IDEMPOTENCY_KEY_REQUIRED", "idempotency_key is required")
        if not isinstance(items, list) or not items:
            raise FileTransferError("UPLOAD_ITEMS_REQUIRED", "at least one upload item is required")

        normalized_items = [_normalize_item(item) for item in items]
        artifact_ids = [item["artifactId"] for item in normalized_items]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise FileTransferError("UPLOAD_DUPLICATE_ARTIFACT", "upload items must use unique artifactId values")
        requested_concurrency = self._normalize_concurrency(concurrency)
        fingerprint = _request_fingerprint(batch_prepare_id, normalized_items)

        if upload_id is None:
            self._validate_batch_prepare_membership(batch_prepare_id, normalized_items)
            for artifact_id in artifact_ids:
                self._validate_artifact_handle(artifact_id)
            job = _UploadJob(
                upload_id=self._new_upload_id(),
                batch_prepare_id=batch_prepare_id,
                order=list(artifact_ids),
                members=set(artifact_ids),
                items={
                    item["artifactId"]: _UploadItem(
                        item_key=item["itemKey"],
                        artifact_id=item["artifactId"],
                        method=item["method"],
                        signed_url=item["url"],
                        headers=dict(item["headers"]),
                        expected_size=item["expectedSize"],
                        expected_sha256=item["expectedSha256"],
                        queued_at=time.perf_counter(),
                    )
                    for item in normalized_items
                },
                concurrency=requested_concurrency,
            )
            job.request_fingerprints[idempotency_key] = fingerprint
            with self._lock:
                self._jobs[job.upload_id] = job
            task = asyncio.create_task(self._run_job_items(job, list(artifact_ids), requested_concurrency))
            self._track_job_task(job, task)
            return self._job_page(job, None)

        with self._lock:
            job = self._jobs.get(upload_id)
            if job is None:
                raise FileTransferError("UPLOAD_JOB_NOT_FOUND", "Unknown upload job")
            if job.batch_prepare_id != batch_prepare_id:
                raise FileTransferError("UPLOAD_BATCH_MISMATCH", "upload job belongs to a different batch_prepare_id")
            prior_fingerprint = job.request_fingerprints.get(idempotency_key)
            if prior_fingerprint is not None:
                if prior_fingerprint != fingerprint:
                    raise FileTransferError(
                        "UPLOAD_IDEMPOTENCY_CONFLICT",
                        "idempotency_key was already used for different upload inputs",
                    )
                return self._job_page(job, None)
            outside = [
                item["artifactId"]
                for item in normalized_items
                if item["artifactId"] not in job.members
                or job.items[item["artifactId"]].item_key != item["itemKey"]
            ]
            if outside:
                raise FileTransferError("UPLOAD_ARTIFACT_NOT_IN_JOB", "retry item/artifact does not belong to the upload job")

        retry_ids: list[str] = []
        with self._lock:
            for item_input in normalized_items:
                artifact_id = item_input["artifactId"]
                item = job.items[artifact_id]
                if item.status == "SUCCEEDED":
                    continue
                if item.status in {"PENDING", "RUNNING"}:
                    continue
                self._validate_artifact_handle(artifact_id)
                item.method = item_input["method"]
                item.signed_url = item_input["url"]
                item.headers = dict(item_input["headers"])
                item.expected_size = item_input["expectedSize"]
                item.expected_sha256 = item_input["expectedSha256"]
                item.status = "PENDING"
                item.result = None
                item.error = None
                item.queued_at = time.perf_counter()
                retry_ids.append(artifact_id)
            job.request_fingerprints[idempotency_key] = fingerprint
            if retry_ids:
                job.cancel_requested = False
                job.status = "RUNNING"

        if retry_ids:
            task = asyncio.create_task(self._run_job_items(job, retry_ids, requested_concurrency))
            self._track_job_task(job, task)
        return self._job_page(job, None)

    def _item_public(self, item: _UploadItem) -> dict[str, Any]:
        value: dict[str, Any] = {
            "itemKey": item.item_key,
            "artifactId": item.artifact_id,
            "status": item.status,
            "attempts": item.attempts,
        }
        if item.result is not None:
            value["timings"] = item.result.get("timings", {})
        if item.error is not None:
            value["error"] = dict(item.error)
        return value

    def _job_page(self, job: _UploadJob, cursor: str | None) -> dict[str, Any]:
        if cursor in {None, ""}:
            offset = 0
        else:
            try:
                offset = int(cursor)
            except (TypeError, ValueError) as exc:
                raise FileTransferError("UPLOAD_CURSOR_INVALID", "cursor must be a non-negative integer offset") from exc
            if offset < 0 or offset > len(job.order):
                raise FileTransferError("UPLOAD_CURSOR_INVALID", "cursor is outside the upload item range")
        end = min(len(job.order), offset + self._page_size)
        page_ids = job.order[offset:end]
        counts = {state: 0 for state in ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")}
        for artifact_id in job.order:
            counts[job.items[artifact_id].status] += 1
        return {
            "uploadId": job.upload_id,
            "batchPrepareId": job.batch_prepare_id,
            "status": job.status,
            "counts": counts,
            "items": [self._item_public(job.items[artifact_id]) for artifact_id in page_ids],
            "nextCursor": str(end) if end < len(job.order) else None,
        }

    def get_upload_prepared_artifacts(self, upload_id: str, cursor: str | None = None) -> dict[str, Any]:
        if not isinstance(upload_id, str) or not upload_id:
            raise FileTransferError("UPLOAD_JOB_ID_REQUIRED", "upload_id is required")
        with self._lock:
            job = self._jobs.get(upload_id)
            if job is None:
                raise FileTransferError("UPLOAD_JOB_NOT_FOUND", "Unknown upload job")
            return self._job_page(job, cursor)

    async def cancel_upload_prepared_artifacts(self, upload_id: str) -> dict[str, Any]:
        if not isinstance(upload_id, str) or not upload_id:
            raise FileTransferError("UPLOAD_JOB_ID_REQUIRED", "upload_id is required")
        with self._lock:
            job = self._jobs.get(upload_id)
            if job is None:
                raise FileTransferError("UPLOAD_JOB_NOT_FOUND", "Unknown upload job")
            if job.status in _TERMINAL_JOB_STATES:
                return self._job_page(job, None)
            job.cancel_requested = True
            tasks = list(job.tasks)
            for item in job.items.values():
                if item.status in {"PENDING", "RUNNING"}:
                    item.status = "CANCELLED"
                    item.error = {"code": "UPLOAD_CANCELLED", "message": "Upload was cancelled"}
                    item.signed_url = ""
                    item.headers.clear()
            self._recompute_job_status(job)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with self._lock:
            self._recompute_job_status(job)
            return self._job_page(job, None)

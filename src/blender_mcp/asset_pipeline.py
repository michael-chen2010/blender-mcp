"""Background Blender orchestration for deterministic asset preparation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Awaitable, Callable, Mapping
import uuid
import weakref

from .blender_runtime import BlenderRuntime, configured_asset_concurrency, resolve_blender_runtime
from .prepared_artifacts import PreparedArtifactError, PreparedArtifactStore

SUPPORTED_PROFILES = {"PREVIEW", "METADATA", "PUBLISH"}
SUPPORTED_SUPPLEMENTAL_VIEWS = {"FRONT", "BACK", "LEFT", "RIGHT", "TOP"}
_DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_LOG_CHARS = 12_000
_DEFAULT_ARTIFACT_STORE = PreparedArtifactStore()
_ASSET_WORKER_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()
_ASSET_WORKER_SEMAPHORE_LOCK = threading.Lock()


class AssetPrepareError(RuntimeError):
    """Stable error raised when an isolated Blender prepare worker fails."""


def get_prepared_artifact_store() -> PreparedArtifactStore:
    """Return the server-process store shared by prepare/upload adapters."""

    return _DEFAULT_ARTIFACT_STORE


def _asset_worker_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _ASSET_WORKER_SEMAPHORE_LOCK:
        semaphore = _ASSET_WORKER_SEMAPHORES.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(configured_asset_concurrency(os.cpu_count()))
            _ASSET_WORKER_SEMAPHORES[loop] = semaphore
        return semaphore


@asynccontextmanager
async def asset_worker_slot():
    """Share one worker limit across prepare and supplemental Blender jobs."""

    semaphore = _asset_worker_semaphore()
    await semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


def _worker_path() -> Path:
    worker = Path(__file__).resolve().parent / "bundled" / "asset_prepare_worker.py"
    if not worker.is_file():
        raise AssetPrepareError(f"Packaged asset prepare worker is missing: {worker}")
    return worker


def _bounded_log(value: str | None) -> str:
    text = value or ""
    if len(text) <= _MAX_LOG_CHARS:
        return text
    return text[-_MAX_LOG_CHARS:]


def _write_job(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _read_worker_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _raise_worker_failure(
    result: dict[str, Any] | None,
    *,
    returncode: int,
    stdout: str,
    stderr: str,
) -> None:
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "WORKER_FAILED")
        message = str(error.get("message") or "worker failed")
        raise AssetPrepareError(f"BLENDER_PREPARE_FAILED: {code}: {message}")
    raise AssetPrepareError(
        "BLENDER_PREPARE_FAILED: background Blender exited with code "
        f"{returncode}; stdout={_bounded_log(stdout)!r}; "
        f"stderr={_bounded_log(stderr)!r}"
    )


def _copy_source_snapshot(source: Path, work_dir: Path, prepare_id: str) -> Path:
    snapshot = work_dir / f"source-{prepare_id}.blend"
    if source.resolve() == snapshot.resolve():
        snapshot = work_dir / f"source-{prepare_id}-copy.blend"
    shutil.copyfile(source, snapshot)
    return snapshot


def _register_result_artifacts(
    *,
    store: PreparedArtifactStore,
    prepare_id: str,
    profile: str,
    work_dir: Path,
    source_snapshot: Path,
    result: dict[str, Any],
    observation_cache_path: Path,
) -> None:
    preview_path = Path(str(result.get("previewPath") or ""))
    payload_path = Path(str(result.get("payloadPath") or "")) if profile == "PUBLISH" else None
    retained = payload_path if payload_path is not None else source_snapshot
    if retained is None or not retained.is_file():
        raise AssetPrepareError("BLENDER_PREPARE_ARTIFACT_MISSING: retained evidence file is missing")
    if not observation_cache_path.is_file():
        raise AssetPrepareError(
            "BLENDER_PREPARE_ARTIFACT_MISSING: prepared observation cache is missing"
        )

    store.register_workspace(
        prepare_id,
        work_dir,
        retained_source_path=retained,
        retained_source_kind="PAYLOAD" if profile == "PUBLISH" else "SOURCE_SNAPSHOT",
        observation_cache_path=observation_cache_path,
    )
    refs = []
    if preview_path.is_file():
        refs.append(
            store.register_artifact(
                prepare_id,
                "PREVIEW",
                preview_path,
                content_type="image/png",
            )
        )
    else:
        raise AssetPrepareError("BLENDER_PREPARE_ARTIFACT_MISSING: MAIN preview is missing")
    if profile == "PUBLISH":
        if payload_path is None or not payload_path.is_file():
            raise AssetPrepareError("BLENDER_PREPARE_ARTIFACT_MISSING: publish payload is missing")
        refs.insert(
            0,
            store.register_artifact(
                prepare_id,
                "PAYLOAD",
                payload_path,
                content_type="application/x-blender",
            ),
        )
    result["artifacts"] = [asdict(ref) for ref in refs]


def prepare_blend_file(
    source_path: str | Path,
    *,
    profile: str = "PREVIEW",
    output_dir: str | Path | None = None,
    runtime: BlenderRuntime | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    source_kind: str = "BLEND_FILE",
    prepare_id: str | None = None,
    artifact_store: PreparedArtifactStore | None = None,
) -> dict[str, Any]:
    """Prepare one source .blend in an isolated background Blender process.

    The GUI Blender command queue is deliberately not involved. The worker opens
    one immutable source snapshot, inspects it, renders the standard MAIN image,
    and for PUBLISH writes and reopens a validated standalone payload.
    """

    source = Path(source_path)
    if not source.is_file():
        raise FileNotFoundError(f"Blend source does not exist: {source}")
    if source.suffix.lower() != ".blend":
        raise ValueError(f"Blend source must end with .blend: {source}")
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(
            f"Unsupported profile {profile!r}; expected one of {sorted(SUPPORTED_PROFILES)}"
        )
    if source_kind not in {"BLEND_FILE", "CURRENT_SELECTION"}:
        raise ValueError("source_kind must be BLEND_FILE or CURRENT_SELECTION")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    resolved_runtime = runtime or resolve_blender_runtime()
    if output_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="blendermcp-prepare-"))
    else:
        work_dir = Path(output_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

    prepare_id_value = prepare_id or str(uuid.uuid4())
    source_snapshot = _copy_source_snapshot(source, work_dir, prepare_id_value)
    job_path = work_dir / "job.json"
    preview_path = work_dir / "main.png"
    payload_path = work_dir / "payload.blend"
    result_path = work_dir / "result.json"
    observation_cache_path = work_dir / "observation-cache.json"
    job = {
        "prepareId": prepare_id_value,
        "profile": profile,
        "sourcePath": str(source_snapshot),
        "sourceKind": source_kind,
        "sourceDisplayName": source.name,
        "previewPath": str(preview_path),
        "payloadPath": str(payload_path),
        "resultPath": str(result_path),
        "observationCachePath": str(observation_cache_path),
    }
    _write_job(job_path, job)

    command = [
        resolved_runtime.executable,
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python",
        str(_worker_path()),
        "--",
        "--job",
        str(job_path),
    ]

    total_started = time.perf_counter()
    spawn_started = time.perf_counter()
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    process_spawn_ms = (time.perf_counter() - spawn_started) * 1000.0

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate()
        raise AssetPrepareError(
            "BLENDER_PREPARE_TIMEOUT: background Blender exceeded "
            f"{timeout:.1f}s; stdout={_bounded_log(stdout)!r}; "
            f"stderr={_bounded_log(stderr)!r}"
        ) from exc

    total_ms = (time.perf_counter() - total_started) * 1000.0
    worker_result = _read_worker_result(result_path)
    if process.returncode != 0:
        _raise_worker_failure(
            worker_result,
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
        )
    if worker_result is None:
        raise AssetPrepareError(
            "BLENDER_PREPARE_RESULT_MISSING: worker exited successfully without valid result.json"
        )

    result = worker_result
    if result.get("status") != "READY":
        error = result.get("error") or {}
        raise AssetPrepareError(
            f"BLENDER_PREPARE_FAILED: {error.get('code', 'WORKER_FAILED')}: "
            f"{error.get('message', 'worker did not return READY')}"
        )

    timings = result.setdefault("timings", {})
    timings["processSpawnMs"] = round(process_spawn_ms, 3)
    timings["totalMs"] = round(max(float(timings.get("totalMs", 0.0)), total_ms), 3)

    store = artifact_store or get_prepared_artifact_store()
    _register_result_artifacts(
        store=store,
        prepare_id=prepare_id_value,
        profile=profile,
        work_dir=work_dir,
        source_snapshot=source_snapshot,
        result=result,
        observation_cache_path=observation_cache_path,
    )
    if profile == "PUBLISH":
        try:
            source_snapshot.unlink()
        except FileNotFoundError:
            pass
    return result


def _decode_process_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


async def _stop_async_process(process: Any) -> tuple[str, str]:
    if process.returncode is None:
        process.kill()
    stdout, stderr = await process.communicate()
    return _decode_process_output(stdout), _decode_process_output(stderr)


async def render_supplemental_view(
    *,
    source_path: str | Path,
    output_path: str | Path,
    view: str,
    prepare_id: str,
    runtime: BlenderRuntime | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Render one deterministic axis view from retained immutable evidence."""

    source = Path(source_path).resolve()
    output = Path(output_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Retained Blend source does not exist: {source}")
    if source.suffix.lower() != ".blend":
        raise ValueError(f"Retained Blend source must end with .blend: {source}")
    if view not in SUPPORTED_SUPPLEMENTAL_VIEWS:
        raise ValueError(
            f"Unsupported supplemental view {view!r}; expected one of {sorted(SUPPORTED_SUPPLEMENTAL_VIEWS)}"
        )
    if not prepare_id:
        raise ValueError("prepare_id is required")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    output.parent.mkdir(parents=True, exist_ok=True)

    resolved_runtime = runtime or resolve_blender_runtime()
    job_path = output.parent / f".{output.stem}.job.json"
    result_path = output.parent / f".{output.stem}.result.json"
    _write_job(
        job_path,
        {
            "mode": "SUPPLEMENTAL_VIEW",
            "prepareId": prepare_id,
            "sourcePath": str(source),
            "previewPath": str(output),
            "resultPath": str(result_path),
            "view": view,
        },
    )
    command = [
        resolved_runtime.executable,
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python",
        str(_worker_path()),
        "--",
        "--job",
        str(job_path),
    ]

    total_started = time.perf_counter()
    process = None
    try:
        async with asset_worker_slot():
            spawn_started = time.perf_counter()
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            process_spawn_ms = (time.perf_counter() - spawn_started) * 1000.0
            try:
                stdout_raw, stderr_raw = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
                stdout = _decode_process_output(stdout_raw)
                stderr = _decode_process_output(stderr_raw)
            except asyncio.TimeoutError as exc:
                stdout, stderr = await _stop_async_process(process)
                raise AssetPrepareError(
                    "BLENDER_SUPPLEMENTAL_TIMEOUT: background Blender exceeded "
                    f"{timeout:.1f}s; stdout={_bounded_log(stdout)!r}; "
                    f"stderr={_bounded_log(stderr)!r}"
                ) from exc
            except asyncio.CancelledError:
                await _stop_async_process(process)
                raise

        result = _read_worker_result(result_path)
        if process.returncode != 0:
            _raise_worker_failure(
                result,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        if result is None or result.get("status") != "READY":
            raise AssetPrepareError(
                "BLENDER_SUPPLEMENTAL_FAILED: worker did not return a READY result"
            )
        if (
            result.get("prepareId") != prepare_id
            or result.get("view") != view
            or Path(str(result.get("previewPath") or "")).resolve() != output
            or not output.is_file()
        ):
            raise AssetPrepareError(
                "BLENDER_SUPPLEMENTAL_RESULT_INVALID: worker result does not match the requested view"
            )

        total_ms = (time.perf_counter() - total_started) * 1000.0
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        public_timings = dict(timings)
        public_timings["processSpawnMs"] = round(process_spawn_ms, 3)
        public_timings["totalMs"] = round(
            max(float(public_timings.get("totalMs", 0.0)), total_ms), 3
        )
        return public_timings
    finally:
        for transient in (job_path, result_path):
            try:
                transient.unlink()
            except FileNotFoundError:
                pass


# Batch prepare jobs are intentionally process-local. AssetPlatform persists the
# logical import batch; Blender-MCP only owns short-lived local worker state.
_BATCH_PAGE_SIZE = 50
_BATCH_MAX_PAGE_SIZE = 100
_BATCH_AI_WINDOW_MAX = 16
_BATCH_TERMINAL_STATES = {"READY", "FAILED", "CANCELLED"}


class AssetBatchError(RuntimeError):
    """Stable validation or scheduling error for local batch preparation."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_item_key(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/").casefold().encode("utf-8")
    return "blend-" + hashlib.sha256(normalized).hexdigest()[:24]


def discover_blend_files(directory: str | Path) -> list[dict[str, str]]:
    """Discover a deterministic local .blend manifest without starting Blender."""

    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise AssetBatchError("BLEND_DISCOVERY_DIRECTORY_NOT_FOUND", "directory must be an existing directory")
    paths = [path.resolve() for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".blend"]
    paths.sort(key=lambda path: (path.relative_to(root).as_posix().casefold(), path.relative_to(root).as_posix()))
    return [
        {
            "itemKey": _stable_item_key(path.relative_to(root).as_posix()),
            "sourceDisplayName": path.name,
            "path": str(path),
            "sourceFingerprint": _sha256_file(path),
        }
        for path in paths
    ]


def _batch_error_from_exception(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, AssetBatchError):
        return exc.code, exc.message
    message = str(exc)
    prefix, separator, detail = message.partition(":")
    if separator and prefix and prefix == prefix.upper() and all(char.isalnum() or char == "_" for char in prefix):
        return prefix, detail.strip() or message
    if isinstance(exc, FileNotFoundError):
        return "BATCH_PREPARE_SOURCE_NOT_FOUND", "Blend source does not exist"
    return "BATCH_PREPARE_FAILED", message or "Batch prepare item failed"


async def _prepare_blend_file_async(
    source_path: str | Path,
    *,
    profile: str,
    runtime: BlenderRuntime | None,
    prepare_id: str,
    output_dir: str | Path,
    artifact_store: PreparedArtifactStore,
    expected_source_fingerprint: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Async batch equivalent of prepare_blend_file using a real subprocess."""

    source = Path(source_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Blend source does not exist: {source}")
    if source.suffix.lower() != ".blend":
        raise AssetBatchError("BATCH_PREPARE_INVALID_SOURCE", "Batch source must end with .blend")
    if profile not in SUPPORTED_PROFILES:
        raise AssetBatchError("BATCH_PREPARE_INVALID_PROFILE", "Unsupported batch prepare profile")
    resolved_runtime = runtime or resolve_blender_runtime()
    work_dir = Path(output_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    source_snapshot = _copy_source_snapshot(source, work_dir, prepare_id)
    if _sha256_file(source_snapshot) != expected_source_fingerprint:
        raise AssetBatchError(
            "BATCH_PREPARE_SOURCE_CHANGED",
            "Source bytes no longer match the registered sourceFingerprint",
        )

    job_path = work_dir / "job.json"
    preview_path = work_dir / "main.png"
    payload_path = work_dir / "payload.blend"
    result_path = work_dir / "result.json"
    observation_cache_path = work_dir / "observation-cache.json"
    _write_job(
        job_path,
        {
            "prepareId": prepare_id,
            "profile": profile,
            "sourcePath": str(source),
            "sourceKind": "BLEND_FILE",
            "sourceDisplayName": source.name,
            "previewPath": str(preview_path),
            "payloadPath": str(payload_path),
            "resultPath": str(result_path),
            "observationCachePath": str(observation_cache_path),
        },
    )
    command = [
        resolved_runtime.executable,
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python",
        str(_worker_path()),
        "--",
        "--job",
        str(job_path),
    ]
    total_started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    spawn_ms = (time.perf_counter() - total_started) * 1000.0
    try:
        stdout_raw, stderr_raw = await asyncio.wait_for(process.communicate(), timeout=timeout)
        stdout = _decode_process_output(stdout_raw)
        stderr = _decode_process_output(stderr_raw)
    except asyncio.TimeoutError as exc:
        stdout, stderr = await _stop_async_process(process)
        raise AssetPrepareError(
            "BLENDER_PREPARE_TIMEOUT: background Blender exceeded "
            f"{timeout:.1f}s; stdout={_bounded_log(stdout)!r}; stderr={_bounded_log(stderr)!r}"
        ) from exc
    except asyncio.CancelledError:
        await _stop_async_process(process)
        raise

    worker_result = _read_worker_result(result_path)
    if process.returncode != 0:
        _raise_worker_failure(worker_result, returncode=process.returncode, stdout=stdout, stderr=stderr)
    if worker_result is None:
        raise AssetPrepareError("BLENDER_PREPARE_RESULT_MISSING: worker exited successfully without valid result.json")
    if worker_result.get("status") != "READY":
        error = worker_result.get("error") or {}
        raise AssetPrepareError(
            f"BLENDER_PREPARE_FAILED: {error.get('code', 'WORKER_FAILED')}: "
            f"{error.get('message', 'worker did not return READY')}"
        )

    timings = worker_result.setdefault("timings", {})
    timings["processSpawnMs"] = round(spawn_ms, 3)
    timings["totalMs"] = round(
        max(float(timings.get("totalMs", 0.0)), (time.perf_counter() - total_started) * 1000.0),
        3,
    )
    _register_result_artifacts(
        store=artifact_store,
        prepare_id=prepare_id,
        profile=profile,
        work_dir=work_dir,
        source_snapshot=source_snapshot,
        result=worker_result,
        observation_cache_path=observation_cache_path,
    )
    if profile == "PUBLISH":
        try:
            source_snapshot.unlink()
        except FileNotFoundError:
            pass
    return worker_result


@dataclass
class _BatchPrepareItem:
    item_key: str
    path: Path
    source_display_name: str
    source_fingerprint: str
    status: str = "PENDING"
    attempts: int = 0
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None


@dataclass
class _BatchPrepareJob:
    batch_prepare_id: str
    profile: str
    order: list[str]
    items: dict[str, _BatchPrepareItem]
    status: str = "RUNNING"
    cancel_requested: bool = False
    request_fingerprints: dict[str, str] = field(default_factory=dict)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    last_concurrency: int = 1


PrepareRunner = Callable[..., Awaitable[dict[str, Any]]]


class AssetPipelineManager:
    """Process-local registry and scheduler for asynchronous batch preparation."""

    def __init__(
        self,
        *,
        artifact_store: PreparedArtifactStore | None = None,
        runtime_provider: Callable[[], BlenderRuntime | None] | None = None,
        prepare_runner: PrepareRunner | None = None,
        page_size: int = _BATCH_PAGE_SIZE,
    ):
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self._artifact_store = artifact_store or get_prepared_artifact_store()
        self._runtime_provider = runtime_provider or resolve_blender_runtime
        self._prepare_runner = prepare_runner or _prepare_blend_file_async
        self._page_size = min(page_size, _BATCH_MAX_PAGE_SIZE)
        self._jobs: dict[str, _BatchPrepareJob] = {}
        self._new_request_index: dict[str, tuple[str, str]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _normalize_fingerprint(value: Any) -> str:
        if not isinstance(value, str) or len(value) != 64:
            raise AssetBatchError("BATCH_PREPARE_SOURCE_FINGERPRINT_INVALID", "sourceFingerprint must be a SHA-256 hex digest")
        normalized = value.lower()
        if any(char not in "0123456789abcdef" for char in normalized):
            raise AssetBatchError("BATCH_PREPARE_SOURCE_FINGERPRINT_INVALID", "sourceFingerprint must be a SHA-256 hex digest")
        return normalized

    def _normalize_items(self, items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list) or not items:
            raise AssetBatchError("BATCH_PREPARE_ITEMS_REQUIRED", "at least one batch prepare item is required")
        normalized: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, Mapping):
                raise AssetBatchError("BATCH_PREPARE_ITEM_INVALID", "each batch prepare item must be an object")
            item_key = raw.get("itemKey")
            path_value = raw.get("path")
            if not isinstance(item_key, str) or not item_key:
                raise AssetBatchError("BATCH_PREPARE_ITEM_KEY_REQUIRED", "itemKey is required")
            if not isinstance(path_value, str) or not path_value:
                raise AssetBatchError("BATCH_PREPARE_PATH_REQUIRED", "path is required")
            path = Path(path_value).expanduser().resolve()
            if path.suffix.lower() != ".blend":
                raise AssetBatchError("BATCH_PREPARE_INVALID_SOURCE", "Batch source must end with .blend")
            normalized.append(
                {
                    "itemKey": item_key,
                    "path": path,
                    "sourceDisplayName": path.name,
                    "sourceFingerprint": self._normalize_fingerprint(raw.get("sourceFingerprint")),
                }
            )
        item_keys = [item["itemKey"] for item in normalized]
        if len(set(item_keys)) != len(item_keys):
            raise AssetBatchError("BATCH_PREPARE_DUPLICATE_ITEM_KEY", "batch prepare items must use unique itemKey values")
        return normalized

    @staticmethod
    def _request_fingerprint(profile: str, items: list[dict[str, Any]]) -> str:
        payload = {
            "profile": profile,
            "items": [
                {
                    "itemKey": item["itemKey"],
                    "path": str(item["path"]),
                    "sourceFingerprint": item["sourceFingerprint"],
                }
                for item in items
            ],
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    @staticmethod
    def _actual_concurrency(requested: int | None) -> int:
        configured = configured_asset_concurrency(os.cpu_count())
        if requested is None:
            return configured
        if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
            raise AssetBatchError("BATCH_PREPARE_CONCURRENCY_INVALID", "concurrency must be a positive integer")
        return min(requested, configured, 12)

    def _new_batch_id(self) -> str:
        value = str(uuid.uuid4())
        with self._lock:
            while value in self._jobs:
                value = str(uuid.uuid4())
        return value

    def _artifacts_are_live(self, item: _BatchPrepareItem) -> bool:
        if item.status != "READY" or not isinstance(item.result, dict):
            return False
        artifacts = item.result.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            return False
        try:
            for raw in artifacts:
                artifact_id = raw.get("artifact_id") if isinstance(raw, dict) else None
                if not isinstance(artifact_id, str):
                    return False
                self._artifact_store.artifact_ref(artifact_id)
        except PreparedArtifactError:
            return False
        return True

    def _recompute_job_status(self, job: _BatchPrepareJob) -> None:
        states = [job.items[item_key].status for item_key in job.order]
        if any(state in {"PENDING", "RUNNING"} for state in states):
            job.status = "RUNNING"
        elif states and all(state == "READY" for state in states):
            job.status = "READY"
        elif states and all(state == "CANCELLED" for state in states):
            job.status = "CANCELLED"
        elif any(state == "READY" for state in states):
            job.status = "PARTIAL"
        elif any(state == "FAILED" for state in states):
            job.status = "FAILED"
        else:
            job.status = "CANCELLED"

    async def _run_item(
        self,
        job: _BatchPrepareJob,
        item: _BatchPrepareItem,
        request_semaphore: asyncio.Semaphore,
    ) -> None:
        work_dir: Path | None = None
        try:
            async with request_semaphore:
                with self._lock:
                    if job.cancel_requested:
                        item.status = "CANCELLED"
                        item.error = {"code": "BATCH_PREPARE_CANCELLED", "message": "Batch prepare item was cancelled"}
                        self._recompute_job_status(job)
                        return
                    item.status = "RUNNING"
                    item.attempts += 1
                    self._recompute_job_status(job)

                if not item.path.is_file():
                    raise FileNotFoundError(str(item.path))
                actual_fingerprint = await asyncio.to_thread(_sha256_file, item.path)
                if actual_fingerprint != item.source_fingerprint:
                    raise AssetBatchError(
                        "BATCH_PREPARE_SOURCE_CHANGED",
                        "Source bytes no longer match the registered sourceFingerprint",
                    )

                work_dir = Path(tempfile.mkdtemp(prefix="blendermcp-batch-prepare-"))
                prepare_id = str(uuid.uuid4())
                runtime = self._runtime_provider()
                if runtime is None:
                    raise AssetBatchError("BLENDER_EXECUTABLE_UNAVAILABLE", "Local Blender executable is unavailable")
                async with asset_worker_slot():
                    result = await self._prepare_runner(
                        item.path,
                        profile=job.profile,
                        runtime=runtime,
                        prepare_id=prepare_id,
                        output_dir=work_dir,
                        artifact_store=self._artifact_store,
                        expected_source_fingerprint=item.source_fingerprint,
                    )
                with self._lock:
                    item.status = "READY"
                    item.result = result
                    item.error = None
                    work_dir = None
                    self._recompute_job_status(job)
        except asyncio.CancelledError:
            with self._lock:
                if item.status != "READY":
                    item.status = "CANCELLED"
                    item.error = {"code": "BATCH_PREPARE_CANCELLED", "message": "Batch prepare item was cancelled"}
                    item.result = None
                    self._recompute_job_status(job)
            raise
        except Exception as exc:
            code, message = _batch_error_from_exception(exc)
            with self._lock:
                item.status = "FAILED"
                item.error = {"code": code, "message": message}
                item.result = None
                self._recompute_job_status(job)
        finally:
            if work_dir is not None:
                shutil.rmtree(work_dir, ignore_errors=True)

    def _track_task(self, job: _BatchPrepareJob, task: asyncio.Task[Any]) -> None:
        with self._lock:
            job.tasks.add(task)

        def done(completed: asyncio.Task[Any]) -> None:
            with self._lock:
                job.tasks.discard(completed)
                self._recompute_job_status(job)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                # Item tasks contain their own stable error conversion; this is defensive.
                pass

        task.add_done_callback(done)

    def _schedule(self, job: _BatchPrepareJob, item_keys: list[str], concurrency: int) -> None:
        request_semaphore = asyncio.Semaphore(concurrency)
        for item_key in item_keys:
            task = asyncio.create_task(self._run_item(job, job.items[item_key], request_semaphore))
            self._track_task(job, task)

    async def start_prepare_blend_assets(
        self,
        items: list[Mapping[str, Any]],
        profile: str,
        idempotency_key: str,
        *,
        batch_prepare_id: str | None = None,
        concurrency: int | None = None,
    ) -> dict[str, Any]:
        if profile not in SUPPORTED_PROFILES:
            raise AssetBatchError("BATCH_PREPARE_INVALID_PROFILE", f"profile must be one of {', '.join(sorted(SUPPORTED_PROFILES))}")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise AssetBatchError("BATCH_PREPARE_IDEMPOTENCY_KEY_REQUIRED", "idempotency_key is required")
        normalized = self._normalize_items(items)
        actual_concurrency = self._actual_concurrency(concurrency)
        fingerprint = self._request_fingerprint(profile, normalized)

        if batch_prepare_id is None:
            if len(normalized) < 2:
                raise AssetBatchError("BATCH_PREPARE_MIN_ITEMS", "initial batch prepare requires at least two items")
            with self._lock:
                prior = self._new_request_index.get(idempotency_key)
                if prior is not None:
                    prior_fingerprint, prior_batch_id = prior
                    if prior_fingerprint != fingerprint:
                        raise AssetBatchError("BATCH_PREPARE_IDEMPOTENCY_CONFLICT", "idempotency_key was already used for different batch inputs")
                    prior_job = self._jobs.get(prior_batch_id)
                    if prior_job is not None:
                        return self._start_result(prior_job)
            batch_id = self._new_batch_id()
            job = _BatchPrepareJob(
                batch_prepare_id=batch_id,
                profile=profile,
                order=[item["itemKey"] for item in normalized],
                items={
                    item["itemKey"]: _BatchPrepareItem(
                        item_key=item["itemKey"],
                        path=item["path"],
                        source_display_name=item["sourceDisplayName"],
                        source_fingerprint=item["sourceFingerprint"],
                    )
                    for item in normalized
                },
                last_concurrency=actual_concurrency,
            )
            job.request_fingerprints[idempotency_key] = fingerprint
            with self._lock:
                self._jobs[batch_id] = job
                self._new_request_index[idempotency_key] = (fingerprint, batch_id)
            self._schedule(job, list(job.order), actual_concurrency)
            return self._start_result(job)

        with self._lock:
            job = self._jobs.get(batch_prepare_id)
            if job is None:
                raise AssetBatchError("BATCH_PREPARE_NOT_FOUND", "Unknown batch_prepare_id")
            if job.profile != profile:
                raise AssetBatchError("BATCH_PREPARE_PROFILE_MISMATCH", "existing batch uses a different profile")
            prior_fingerprint = job.request_fingerprints.get(idempotency_key)
            if prior_fingerprint is not None:
                if prior_fingerprint != fingerprint:
                    raise AssetBatchError("BATCH_PREPARE_IDEMPOTENCY_CONFLICT", "idempotency_key was already used for different retry inputs")
                return self._start_result(job)
            for candidate in normalized:
                existing = job.items.get(candidate["itemKey"])
                if existing is None:
                    raise AssetBatchError("BATCH_PREPARE_ITEM_NOT_IN_MANIFEST", "retry item is not part of the original batch manifest")
                if existing.path != candidate["path"]:
                    raise AssetBatchError("BATCH_PREPARE_SOURCE_PATH_MISMATCH", "retry item path differs from the original batch manifest")
                if existing.source_fingerprint != candidate["sourceFingerprint"]:
                    raise AssetBatchError("BATCH_PREPARE_SOURCE_FINGERPRINT_MISMATCH", "retry sourceFingerprint differs from the original batch manifest")

            retry_keys: list[str] = []
            for candidate in normalized:
                existing = job.items[candidate["itemKey"]]
                if existing.status == "READY" and self._artifacts_are_live(existing):
                    continue
                if existing.status in {"PENDING", "RUNNING"}:
                    continue
                existing.status = "PENDING"
                existing.result = None
                existing.error = None
                retry_keys.append(existing.item_key)
            job.request_fingerprints[idempotency_key] = fingerprint
            job.cancel_requested = False
            job.last_concurrency = actual_concurrency
            if retry_keys:
                job.status = "RUNNING"

        if retry_keys:
            self._schedule(job, retry_keys, actual_concurrency)
        return self._start_result(job)

    def _start_result(self, job: _BatchPrepareJob) -> dict[str, Any]:
        self._recompute_job_status(job)
        return {
            "batchPrepareId": job.batch_prepare_id,
            "status": "RUNNING" if job.status == "RUNNING" else job.status,
            "itemCount": len(job.order),
            "concurrency": job.last_concurrency,
        }

    @staticmethod
    def _public_result(result: dict[str, Any]) -> dict[str, Any]:
        public = {
            key: result[key]
            for key in ("prepareId", "profile", "observation", "artifacts", "warnings", "timings", "validation")
            if key in result
        }
        return public

    def _item_public(self, item: _BatchPrepareItem, *, include_result: bool) -> dict[str, Any]:
        value: dict[str, Any] = {
            "itemKey": item.item_key,
            "sourceDisplayName": item.source_display_name,
            "sourceFingerprint": item.source_fingerprint,
            "status": item.status,
            "attempts": item.attempts,
        }
        if item.status == "READY" and item.result is not None:
            if include_result:
                value["result"] = self._public_result(item.result)
            else:
                prepare_id = item.result.get("prepareId")
                if isinstance(prepare_id, str):
                    value["prepareId"] = prepare_id
                timings = item.result.get("timings")
                if isinstance(timings, Mapping):
                    value["timings"] = dict(timings)
        if item.error is not None:
            value["error"] = dict(item.error)
        return value

    def get_prepare_blend_assets(
        self,
        batch_prepare_id: str,
        cursor: str | None = None,
        limit: int | None = None,
        mode: str = "LEGACY_FULL",
    ) -> dict[str, Any]:
        if not isinstance(batch_prepare_id, str) or not batch_prepare_id:
            raise AssetBatchError("BATCH_PREPARE_ID_REQUIRED", "batch_prepare_id is required")
        if mode not in {"STATUS", "LEGACY_FULL"}:
            raise AssetBatchError(
                "BATCH_PREPARE_MODE_INVALID",
                "mode must be STATUS or LEGACY_FULL",
            )
        page_limit = self._page_size if limit is None else limit
        if isinstance(page_limit, bool) or not isinstance(page_limit, int) or page_limit <= 0 or page_limit > _BATCH_MAX_PAGE_SIZE:
            raise AssetBatchError("BATCH_PREPARE_LIMIT_INVALID", f"limit must be between 1 and {_BATCH_MAX_PAGE_SIZE}")
        if cursor in {None, ""}:
            offset = 0
        else:
            try:
                offset = int(cursor)
            except (TypeError, ValueError) as exc:
                raise AssetBatchError("BATCH_PREPARE_CURSOR_INVALID", "cursor must be a non-negative integer offset") from exc
        with self._lock:
            job = self._jobs.get(batch_prepare_id)
            if job is None:
                raise AssetBatchError("BATCH_PREPARE_NOT_FOUND", "Unknown batch_prepare_id")
            if offset < 0 or offset > len(job.order):
                raise AssetBatchError("BATCH_PREPARE_CURSOR_INVALID", "cursor is outside the batch item range")
            self._recompute_job_status(job)
            end = min(len(job.order), offset + page_limit)
            states = ("PENDING", "RUNNING", "READY", "FAILED", "CANCELLED")
            counts = {state: 0 for state in states}
            for item_key in job.order:
                counts[job.items[item_key].status] += 1
            return {
                "batchPrepareId": job.batch_prepare_id,
                "status": job.status,
                "mode": mode,
                "counts": counts,
                "items": [
                    self._item_public(
                        job.items[item_key],
                        include_result=mode == "LEGACY_FULL",
                    )
                    for item_key in job.order[offset:end]
                ],
                "nextCursor": str(end) if end < len(job.order) else None,
            }

    def get_prepared_asset_window(
        self,
        batch_prepare_id: str,
        items: Any,
    ) -> dict[str, Any]:
        if not isinstance(batch_prepare_id, str) or not batch_prepare_id:
            raise AssetBatchError("BATCH_PREPARE_ID_REQUIRED", "batch_prepare_id is required")
        if not isinstance(items, list) or not items:
            raise AssetBatchError(
                "BATCH_PREPARE_WINDOW_ITEMS_REQUIRED",
                "at least one prepared item is required",
            )
        if len(items) > _BATCH_AI_WINDOW_MAX:
            raise AssetBatchError(
                "BATCH_PREPARE_WINDOW_TOO_LARGE",
                f"prepared asset window cannot exceed {_BATCH_AI_WINDOW_MAX} items",
            )

        normalized: list[tuple[str, str]] = []
        seen_item_keys: set[str] = set()
        for raw in items:
            if not isinstance(raw, Mapping):
                raise AssetBatchError(
                    "BATCH_PREPARE_WINDOW_ITEM_INVALID",
                    "each prepared asset window item must be an object",
                )
            item_key = raw.get("itemKey")
            prepare_id = raw.get("prepareId")
            if not isinstance(item_key, str) or not item_key:
                raise AssetBatchError(
                    "BATCH_PREPARE_WINDOW_ITEM_KEY_REQUIRED",
                    "itemKey is required",
                )
            if not isinstance(prepare_id, str) or not prepare_id:
                raise AssetBatchError(
                    "BATCH_PREPARE_WINDOW_PREPARE_ID_REQUIRED",
                    "prepareId is required",
                )
            if item_key in seen_item_keys:
                raise AssetBatchError(
                    "BATCH_PREPARE_WINDOW_DUPLICATE_ITEM",
                    "prepared asset window itemKey values must be unique",
                )
            seen_item_keys.add(item_key)
            normalized.append((item_key, prepare_id))

        with self._lock:
            job = self._jobs.get(batch_prepare_id)
            if job is None:
                raise AssetBatchError("BATCH_PREPARE_NOT_FOUND", "Unknown batch_prepare_id")

            public_items: list[dict[str, Any]] = []
            for item_key, prepare_id in normalized:
                item = job.items.get(item_key)
                if item is None:
                    raise AssetBatchError(
                        "BATCH_PREPARE_WINDOW_ITEM_NOT_FOUND",
                        f"itemKey is not part of batch: {item_key}",
                    )
                if item.status != "READY" or not isinstance(item.result, dict):
                    raise AssetBatchError(
                        "BATCH_PREPARE_WINDOW_ITEM_NOT_READY",
                        f"itemKey is not READY: {item_key}",
                    )
                current_prepare_id = item.result.get("prepareId")
                if current_prepare_id != prepare_id:
                    raise AssetBatchError(
                        "BATCH_PREPARE_WINDOW_IDENTITY_MISMATCH",
                        f"prepareId does not match the READY item: {item_key}",
                    )
                public_items.append(
                    {
                        "itemKey": item.item_key,
                        "sourceDisplayName": item.source_display_name,
                        "sourceFingerprint": item.source_fingerprint,
                        "result": self._public_result(item.result),
                    }
                )

            return {
                "batchPrepareId": job.batch_prepare_id,
                "items": public_items,
            }

    async def cancel_prepare_blend_assets(self, batch_prepare_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(batch_prepare_id)
            if job is None:
                raise AssetBatchError("BATCH_PREPARE_NOT_FOUND", "Unknown batch_prepare_id")
            job.cancel_requested = True
            tasks = list(job.tasks)
            for item in job.items.values():
                if item.status in {"PENDING", "RUNNING"}:
                    item.status = "CANCELLED"
                    item.error = {"code": "BATCH_PREPARE_CANCELLED", "message": "Batch prepare item was cancelled"}
                    item.result = None
            self._recompute_job_status(job)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return self.get_prepare_blend_assets(batch_prepare_id, limit=_BATCH_MAX_PAGE_SIZE)

    def resolve_upload_manifest(self, batch_prepare_id: str) -> dict[str, set[str]] | None:
        """Return item->artifact membership for Task 6 upload validation."""

        with self._lock:
            job = self._jobs.get(batch_prepare_id)
            if job is None:
                return None
            manifest: dict[str, set[str]] = {}
            for item_key in job.order:
                item = job.items[item_key]
                artifact_ids: set[str] = set()
                if item.status == "READY" and isinstance(item.result, dict):
                    for raw in item.result.get("artifacts", []):
                        if isinstance(raw, dict) and isinstance(raw.get("artifact_id"), str):
                            artifact_ids.add(raw["artifact_id"])
                manifest[item_key] = artifact_ids
            return manifest

    async def shutdown(self) -> None:
        """Cancel all local worker tasks so server shutdown cannot orphan Blender."""

        with self._lock:
            jobs = list(self._jobs.values())
            tasks = [task for job in jobs for task in job.tasks]
            for job in jobs:
                job.cancel_requested = True
                for item in job.items.values():
                    if item.status in {"PENDING", "RUNNING"}:
                        item.status = "CANCELLED"
                        item.error = {"code": "BATCH_PREPARE_CANCELLED", "message": "Batch prepare item was cancelled"}
            for task in tasks:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

"""Background Blender orchestration for deterministic asset preparation."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any
import uuid
import weakref

from .blender_runtime import BlenderRuntime, configured_asset_concurrency, resolve_blender_runtime
from .prepared_artifacts import PreparedArtifactStore

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

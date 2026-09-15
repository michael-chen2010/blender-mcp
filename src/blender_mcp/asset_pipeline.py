"""Background Blender orchestration for deterministic asset preparation."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from typing import Any
import uuid

from .blender_runtime import BlenderRuntime, resolve_blender_runtime
from .prepared_artifacts import PreparedArtifactStore

SUPPORTED_PROFILES = {"PREVIEW", "METADATA", "PUBLISH"}
_DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_LOG_CHARS = 12_000
_DEFAULT_ARTIFACT_STORE = PreparedArtifactStore()


class AssetPrepareError(RuntimeError):
    """Stable error raised when an isolated Blender prepare worker fails."""


def get_prepared_artifact_store() -> PreparedArtifactStore:
    """Return the server-process store shared by prepare/upload adapters."""

    return _DEFAULT_ARTIFACT_STORE


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
) -> None:
    preview_path = Path(str(result.get("previewPath") or ""))
    payload_path = Path(str(result.get("payloadPath") or "")) if profile == "PUBLISH" else None
    retained = payload_path if payload_path is not None else source_snapshot
    if retained is None or not retained.is_file():
        raise AssetPrepareError("BLENDER_PREPARE_ARTIFACT_MISSING: retained evidence file is missing")

    store.register_workspace(
        prepare_id,
        work_dir,
        retained_source_path=retained,
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
    job = {
        "prepareId": prepare_id_value,
        "profile": profile,
        "sourcePath": str(source_snapshot),
        "sourceKind": source_kind,
        "sourceDisplayName": source.name,
        "previewPath": str(preview_path),
        "payloadPath": str(payload_path),
        "resultPath": str(result_path),
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
    )
    if profile == "PUBLISH":
        try:
            source_snapshot.unlink()
        except FileNotFoundError:
            pass
    return result

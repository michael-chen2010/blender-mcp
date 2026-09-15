"""Background Blender orchestration for deterministic asset preparation."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any
import uuid

from .blender_runtime import BlenderRuntime, resolve_blender_runtime

SUPPORTED_PROFILES = {"PREVIEW", "METADATA", "PUBLISH"}
_DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_LOG_CHARS = 12_000


class AssetPrepareError(RuntimeError):
    """Stable error raised when an isolated Blender prepare worker fails."""


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


def prepare_blend_file(
    source_path: str | Path,
    *,
    profile: str = "PREVIEW",
    output_dir: str | Path | None = None,
    runtime: BlenderRuntime | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    source_kind: str = "BLEND_FILE",
    prepare_id: str | None = None,
) -> dict[str, Any]:
    """Prepare one source .blend in an isolated background Blender process.

    The GUI Blender command queue is deliberately not involved. The worker opens
    the source exactly once, inspects it, creates the standard MAIN preview in
    its isolated process, and writes a bounded JSON result for the MCP server.
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
    job_path = work_dir / "job.json"
    preview_path = work_dir / "main.png"
    result_path = work_dir / "result.json"
    job = {
        "prepareId": prepare_id_value,
        "profile": profile,
        "sourcePath": str(source),
        "sourceKind": source_kind,
        "sourceDisplayName": source.name,
        "previewPath": str(preview_path),
        "resultPath": str(result_path),
    }
    _write_job(job_path, job)

    command = [
        resolved_runtime.executable,
        "--background",
        "--factory-startup",
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
    if process.returncode != 0:
        raise AssetPrepareError(
            "BLENDER_PREPARE_FAILED: background Blender exited with code "
            f"{process.returncode}; stdout={_bounded_log(stdout)!r}; "
            f"stderr={_bounded_log(stderr)!r}"
        )
    if not result_path.is_file():
        raise AssetPrepareError(
            "BLENDER_PREPARE_RESULT_MISSING: worker exited successfully without result.json"
        )

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetPrepareError(
            f"BLENDER_PREPARE_RESULT_INVALID: could not read {result_path}: {exc}"
        ) from exc

    if result.get("status") != "READY":
        error = result.get("error") or {}
        raise AssetPrepareError(
            f"BLENDER_PREPARE_FAILED: {error.get('code', 'WORKER_FAILED')}: "
            f"{error.get('message', 'worker did not return READY')}"
        )

    timings = result.setdefault("timings", {})
    timings["processSpawnMs"] = round(process_spawn_ms, 3)
    timings["totalMs"] = round(max(float(timings.get("totalMs", 0.0)), total_ms), 3)
    return result

"""Local Blender runtime resolution and worker concurrency policy."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
from typing import Callable, Literal, Mapping

BLENDER_EXECUTABLE_ENV = "BLENDERMCP_BLENDER_EXECUTABLE"
ASSET_CONCURRENCY_ENV = "BLENDERMCP_ASSET_MAX_CONCURRENCY"
ASSET_HARD_CAP = 12
SUPPORTED_ASSET_PROFILES = ("PREVIEW", "METADATA", "PUBLISH")


@dataclass(frozen=True)
class BlenderRuntime:
    executable: str
    source: Literal["ENV", "LOCAL_ADDON", "PATH"]


def _is_local_blender_host(host: str | None) -> bool:
    if host is None:
        return True
    normalized = host.strip().lower()
    return normalized in {"", "localhost", "127.0.0.1", "::1"}


def _validated_file(path: str | None) -> str | None:
    if not path:
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        return None
    return str(candidate)


def resolve_blender_runtime(
    *,
    env: Mapping[str, str] | None = None,
    addon_binary_path: str | None = None,
    blender_host: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> BlenderRuntime:
    """Resolve the local Blender executable without contacting the GUI Add-on.

    An explicitly configured executable is authoritative. A path reported by the
    Add-on is considered only when the configured Blender host is local; paths
    reported by a remote Blender instance are never treated as local files.
    """

    values = os.environ if env is None else env
    explicit = values.get(BLENDER_EXECUTABLE_ENV)
    if explicit:
        resolved = _validated_file(explicit)
        if resolved is None:
            raise FileNotFoundError(
                f"Blender executable from {BLENDER_EXECUTABLE_ENV} does not exist: {explicit}"
            )
        return BlenderRuntime(resolved, "ENV")

    if _is_local_blender_host(blender_host):
        resolved = _validated_file(addon_binary_path)
        if resolved is not None:
            return BlenderRuntime(resolved, "LOCAL_ADDON")

    path_candidate = which("blender")
    resolved = _validated_file(path_candidate)
    if resolved is not None:
        return BlenderRuntime(resolved, "PATH")

    raise FileNotFoundError(
        "Blender executable is unavailable. Set BLENDERMCP_BLENDER_EXECUTABLE, "
        "connect a local Blender Add-on that reports bpy.app.binary_path, or add "
        "Blender to PATH."
    )


def recommended_asset_concurrency(cpu_count: int | None) -> int:
    """Return the default global asset-worker concurrency for this machine."""

    cpus = cpu_count if cpu_count is not None and cpu_count > 0 else 1
    return min(ASSET_HARD_CAP, max(1, cpus // 2))


def configured_asset_concurrency(
    cpu_count: int | None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Return the configured worker limit, clamped to the supported hard cap."""

    recommended = recommended_asset_concurrency(cpu_count)
    values = os.environ if env is None else env
    raw = values.get(ASSET_CONCURRENCY_ENV)
    if raw is None or not raw.strip():
        return recommended
    try:
        requested = int(raw)
    except ValueError:
        return recommended
    return min(ASSET_HARD_CAP, max(1, requested))

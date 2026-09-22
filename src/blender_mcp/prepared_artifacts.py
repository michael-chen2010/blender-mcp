"""Short-lived capability store for files created by asset preparation jobs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import secrets
import shutil
import threading
import time
from typing import Callable, Iterator, Literal

ArtifactKind = Literal["PAYLOAD", "PREVIEW"]
RetainedSourceKind = Literal["PAYLOAD", "SOURCE_SNAPSHOT"]
_DEFAULT_TTL_SECONDS = 60 * 60


class PreparedArtifactError(RuntimeError):
    """Stable local capability error for prepared artifact resolution."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class PreparedArtifactRef:
    artifact_id: str
    kind: ArtifactKind
    size: int
    sha256: str
    content_type: str
    expires_at: str


@dataclass
class _ArtifactRecord:
    ref: PreparedArtifactRef
    prepare_id: str
    path: Path
    expires_at_epoch: float


@dataclass
class _WorkspaceRecord:
    path: Path
    expires_at_epoch: float
    artifact_ids: set[str] = field(default_factory=set)
    lease_count: int = 0
    retained_source_path: Path | None = None
    retained_source_kind: RetainedSourceKind | None = None
    retained_source_size: int | None = None
    retained_source_sha256: str | None = None
    observation_cache_path: Path | None = None
    observation_cache_size: int | None = None
    observation_cache_sha256: str | None = None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_utc(epoch_seconds: float) -> str:
    return (
        datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _inside(path: Path, workspace: Path) -> bool:
    try:
        path.relative_to(workspace)
    except ValueError:
        return False
    return True


class PreparedArtifactStore:
    """In-memory registry of opaque handles backed by prepare workspaces.

    Paths never appear in the public artifact reference. Resolution revalidates
    containment, exact size and SHA-256 so a handle cannot silently refer to
    replaced bytes. Workspace cleanup is delayed while a lease is active.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._workspaces: dict[str, _WorkspaceRecord] = {}
        self._artifacts: dict[str, _ArtifactRecord] = {}

    def register_workspace(
        self,
        prepare_id: str,
        workspace: str | Path,
        *,
        retained_source_path: str | Path | None = None,
        retained_source_kind: RetainedSourceKind = "SOURCE_SNAPSHOT",
        observation_cache_path: str | Path | None = None,
    ) -> None:
        if not prepare_id:
            raise ValueError("prepare_id is required")
        resolved_workspace = Path(workspace).resolve()
        if not resolved_workspace.is_dir():
            raise PreparedArtifactError(
                "PREPARED_ARTIFACT_WORKSPACE_MISSING",
                f"Prepare workspace does not exist for {prepare_id}",
            )
        if retained_source_kind not in {"PAYLOAD", "SOURCE_SNAPSHOT"}:
            raise ValueError("retained_source_kind must be PAYLOAD or SOURCE_SNAPSHOT")

        retained: Path | None = None
        retained_size: int | None = None
        retained_sha256: str | None = None
        if retained_source_path is not None:
            retained = Path(retained_source_path).resolve()
            if not retained.is_file() or not _inside(retained, resolved_workspace):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_INVALID_PATH",
                    "Retained source must be a file inside the prepare workspace",
                )
            retained_size = retained.stat().st_size
            retained_sha256 = _sha256_file(retained)

        cache: Path | None = None
        cache_size: int | None = None
        cache_sha256: str | None = None
        if observation_cache_path is not None:
            cache = Path(observation_cache_path).resolve()
            if not cache.is_file() or not _inside(cache, resolved_workspace):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_INVALID_PATH",
                    "Observation cache must be a file inside the prepare workspace",
                )
            cache_size = cache.stat().st_size
            cache_sha256 = _sha256_file(cache)
        with self._lock:
            if prepare_id in self._workspaces:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_WORKSPACE_EXISTS",
                    f"Prepare workspace already registered: {prepare_id}",
                )
            self._workspaces[prepare_id] = _WorkspaceRecord(
                path=resolved_workspace,
                expires_at_epoch=self._clock() + self._ttl_seconds,
                retained_source_path=retained,
                retained_source_kind=retained_source_kind if retained is not None else None,
                retained_source_size=retained_size,
                retained_source_sha256=retained_sha256,
                observation_cache_path=cache,
                observation_cache_size=cache_size,
                observation_cache_sha256=cache_sha256,
            )

    def register_artifact(
        self,
        prepare_id: str,
        kind: ArtifactKind,
        path: str | Path,
        *,
        content_type: str,
    ) -> PreparedArtifactRef:
        if kind not in {"PAYLOAD", "PREVIEW"}:
            raise ValueError("kind must be PAYLOAD or PREVIEW")
        with self._lock:
            workspace = self._workspaces.get(prepare_id)
            if workspace is None:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_WORKSPACE_NOT_FOUND",
                    f"Unknown prepare workspace: {prepare_id}",
                )
            resolved_path = Path(path).resolve()
            if not resolved_path.is_file() or not _inside(resolved_path, workspace.path):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_INVALID_PATH",
                    "Artifact must be a file inside its registered prepare workspace",
                )
            size = resolved_path.stat().st_size
            sha256 = _sha256_file(resolved_path)
            expires_epoch = self._clock() + self._ttl_seconds
            artifact_id = secrets.token_urlsafe(24)
            while artifact_id in self._artifacts:
                artifact_id = secrets.token_urlsafe(24)
            ref = PreparedArtifactRef(
                artifact_id=artifact_id,
                kind=kind,
                size=size,
                sha256=sha256,
                content_type=content_type,
                expires_at=_iso_utc(expires_epoch),
            )
            self._artifacts[artifact_id] = _ArtifactRecord(
                ref=ref,
                prepare_id=prepare_id,
                path=resolved_path,
                expires_at_epoch=expires_epoch,
            )
            workspace.artifact_ids.add(artifact_id)
            workspace.expires_at_epoch = max(workspace.expires_at_epoch, expires_epoch)
            return ref

    def resolve(self, artifact_id: str) -> Path:
        with self._lock:
            record = self._artifacts.get(artifact_id)
            if record is None:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_NOT_FOUND",
                    "Unknown prepared artifact id",
                )
            if self._clock() >= record.expires_at_epoch:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_EXPIRED",
                    "Prepared artifact has expired",
                )
            workspace = self._workspaces.get(record.prepare_id)
            if workspace is None:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_NOT_FOUND",
                    "Prepared artifact workspace is unavailable",
                )
            current_path = record.path.resolve()
            if not current_path.is_file() or not _inside(current_path, workspace.path):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_INVALID_PATH",
                    "Prepared artifact path is no longer valid",
                )
            stat = current_path.stat()
            if stat.st_size != record.ref.size or _sha256_file(current_path) != record.ref.sha256:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_CHECKSUM_MISMATCH",
                    "Prepared artifact bytes no longer match the registered identity",
                )
            return current_path

    def artifact_prepare_id(self, artifact_id: str) -> str:
        """Return the owning prepare id for a live opaque artifact handle."""

        with self._lock:
            record = self._artifacts.get(artifact_id)
            if record is None:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_NOT_FOUND",
                    "Unknown prepared artifact id",
                )
            if self._clock() >= record.expires_at_epoch:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_EXPIRED",
                    "Prepared artifact has expired",
                )
            self._workspace_for_read(record.prepare_id)
            return record.prepare_id

    def artifact_ref(self, artifact_id: str) -> PreparedArtifactRef:
        """Return immutable public identity for a live opaque artifact handle."""

        with self._lock:
            prepare_id = self.artifact_prepare_id(artifact_id)
            record = self._artifacts.get(artifact_id)
            if record is None or record.prepare_id != prepare_id:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_NOT_FOUND",
                    "Prepared artifact is unavailable",
                )
            return record.ref

    def _workspace_for_read(self, prepare_id: str) -> _WorkspaceRecord:
        workspace = self._workspaces.get(prepare_id)
        if workspace is None:
            raise PreparedArtifactError(
                "PREPARED_ARTIFACT_NOT_FOUND",
                f"Unknown prepare workspace: {prepare_id}",
            )
        if self._clock() >= workspace.expires_at_epoch:
            raise PreparedArtifactError(
                "PREPARED_ARTIFACT_EXPIRED",
                "Prepare workspace has expired",
            )
        return workspace

    def retained_source_path(self, prepare_id: str) -> Path:
        with self._lock:
            workspace = self._workspace_for_read(prepare_id)
            path = workspace.retained_source_path
            if path is None or not path.is_file() or not _inside(path.resolve(), workspace.path):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_NOT_FOUND",
                    "Prepare workspace has no retained source snapshot",
                )
            resolved = path.resolve()
            stat = resolved.stat()
            if (
                workspace.retained_source_size is None
                or workspace.retained_source_sha256 is None
                or stat.st_size != workspace.retained_source_size
                or _sha256_file(resolved) != workspace.retained_source_sha256
            ):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_CHECKSUM_MISMATCH",
                    "Retained evidence bytes no longer match the registered identity",
                )
            return resolved

    def evidence_identity(self, prepare_id: str) -> dict[str, str | int]:
        with self._lock:
            workspace = self._workspace_for_read(prepare_id)
            path = self.retained_source_path(prepare_id)
            if workspace.retained_source_kind is None:
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_NOT_FOUND",
                    "Prepare workspace has no retained evidence identity",
                )
            return {
                "prepareId": prepare_id,
                "kind": workspace.retained_source_kind,
                "size": path.stat().st_size,
                "sha256": workspace.retained_source_sha256 or _sha256_file(path),
            }

    def observation_cache_path(self, prepare_id: str) -> Path:
        with self._lock:
            workspace = self._workspace_for_read(prepare_id)
            path = workspace.observation_cache_path
            if path is None or not path.is_file() or not _inside(path.resolve(), workspace.path):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_NOT_FOUND",
                    "Prepare workspace has no observation cache",
                )
            resolved = path.resolve()
            stat = resolved.stat()
            if (
                workspace.observation_cache_size is None
                or workspace.observation_cache_sha256 is None
                or stat.st_size != workspace.observation_cache_size
                or _sha256_file(resolved) != workspace.observation_cache_sha256
            ):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_CHECKSUM_MISMATCH",
                    "Observation cache bytes no longer match the registered identity",
                )
            return resolved

    def renew_prepare(self, prepare_id: str) -> dict[str, str | int]:
        """Extend one still-live prepare workspace without reviving expired handles."""

        with self._lock:
            workspace = self._workspace_for_read(prepare_id)
            now = self._clock()
            expires_epoch = now + self._ttl_seconds
            workspace.expires_at_epoch = max(workspace.expires_at_epoch, expires_epoch)

            renewed_artifacts = 0
            for artifact_id in workspace.artifact_ids:
                record = self._artifacts.get(artifact_id)
                if record is None or record.prepare_id != prepare_id:
                    continue
                if now >= record.expires_at_epoch:
                    continue
                record.expires_at_epoch = max(record.expires_at_epoch, expires_epoch)
                record.ref = replace(
                    record.ref,
                    expires_at=_iso_utc(record.expires_at_epoch),
                )
                renewed_artifacts += 1

            return {
                "prepareId": prepare_id,
                "expiresAt": _iso_utc(workspace.expires_at_epoch),
                "renewedArtifacts": renewed_artifacts,
            }

    @contextmanager
    def lease(self, prepare_id: str) -> Iterator[Path]:
        with self._lock:
            workspace = self._workspace_for_read(prepare_id)
            workspace.lease_count += 1
            path = workspace.path
        try:
            yield path
        finally:
            with self._lock:
                current = self._workspaces.get(prepare_id)
                if current is not None:
                    current.lease_count = max(0, current.lease_count - 1)

    def cleanup_expired(self) -> list[str]:
        now = self._clock()
        removed: list[str] = []
        with self._lock:
            for prepare_id, workspace in list(self._workspaces.items()):
                if workspace.lease_count > 0 or now < workspace.expires_at_epoch:
                    continue
                for artifact_id in workspace.artifact_ids:
                    self._artifacts.pop(artifact_id, None)
                try:
                    shutil.rmtree(workspace.path)
                except FileNotFoundError:
                    pass
                self._workspaces.pop(prepare_id, None)
                removed.append(prepare_id)
        return sorted(removed)

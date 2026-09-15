"""Short-lived capability store for files created by asset preparation jobs."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import secrets
import shutil
import threading
import time
from typing import Callable, Iterator, Literal

ArtifactKind = Literal["PAYLOAD", "PREVIEW"]
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
    ) -> None:
        if not prepare_id:
            raise ValueError("prepare_id is required")
        resolved_workspace = Path(workspace).resolve()
        if not resolved_workspace.is_dir():
            raise PreparedArtifactError(
                "PREPARED_ARTIFACT_WORKSPACE_MISSING",
                f"Prepare workspace does not exist for {prepare_id}",
            )
        retained: Path | None = None
        if retained_source_path is not None:
            retained = Path(retained_source_path).resolve()
            if not retained.is_file() or not _inside(retained, resolved_workspace):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_INVALID_PATH",
                    "Retained source must be a file inside the prepare workspace",
                )
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

    def retained_source_path(self, prepare_id: str) -> Path:
        with self._lock:
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
            path = workspace.retained_source_path
            if path is None or not path.is_file() or not _inside(path.resolve(), workspace.path):
                raise PreparedArtifactError(
                    "PREPARED_ARTIFACT_NOT_FOUND",
                    "Prepare workspace has no retained source snapshot",
                )
            return path.resolve()

    @contextmanager
    def lease(self, prepare_id: str) -> Iterator[Path]:
        with self._lock:
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

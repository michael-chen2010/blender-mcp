"""Cached prepared-asset inspection and immutable supplemental view orchestration."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import threading
from typing import Any, Awaitable, Callable

from .prepared_artifacts import PreparedArtifactError, PreparedArtifactStore

SUPPORTED_SECTIONS = ("STRUCTURE", "GEOMETRY", "MATERIALS", "DEFORMATION")
SUPPORTED_VIEWS = ("FRONT", "BACK", "LEFT", "RIGHT", "TOP")
_DEFAULT_PAGE_LIMIT = 20
_MAX_PAGE_LIMIT = 100

Renderer = Callable[..., Awaitable[dict[str, Any]]]


class PreparedObservationError(RuntimeError):
    """Stable error raised by cached inspection/supplemental rendering."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _from_artifact_error(exc: PreparedArtifactError) -> PreparedObservationError:
    text = str(exc)
    prefix = f"{exc.code}: "
    message = text[len(prefix) :] if text.startswith(prefix) else text
    return PreparedObservationError(exc.code, message)


def _public_artifact(ref: Any, path: Path) -> dict[str, Any]:
    value = asdict(ref)
    return {
        "artifactId": value["artifact_id"],
        "kind": value["kind"],
        "fileName": path.name,
        "size": value["size"],
        "sha256": value["sha256"],
        "mimeType": value["content_type"],
        "expiresAt": value["expires_at"],
    }


def _parse_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except (TypeError, ValueError) as exc:
        raise PreparedObservationError(
            "PREPARED_OBSERVATION_INVALID_CURSOR", "cursor must be a non-negative integer offset"
        ) from exc
    if value < 0 or str(value) != cursor.strip():
        raise PreparedObservationError(
            "PREPARED_OBSERVATION_INVALID_CURSOR", "cursor must be a non-negative integer offset"
        )
    return value


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > _MAX_PAGE_LIMIT:
        raise PreparedObservationError(
            "PREPARED_OBSERVATION_INVALID_LIMIT",
            f"limit must be an integer between 1 and {_MAX_PAGE_LIMIT}",
        )
    return limit


def _section_details(section: str, value: dict[str, Any]) -> tuple[dict[str, Any], list[Any]]:
    if section == "STRUCTURE":
        bounded = value.get("objects")
        if not isinstance(bounded, dict) or not isinstance(bounded.get("items"), list):
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_CACHE_INVALID", "STRUCTURE cache is missing objects.items"
            )
        summary = {key: item for key, item in value.items() if key != "objects"}
        return summary, list(bounded["items"])
    if section == "MATERIALS":
        bounded = value.get("items")
        if not isinstance(bounded, dict) or not isinstance(bounded.get("items"), list):
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_CACHE_INVALID", "MATERIALS cache is missing items.items"
            )
        summary = {key: item for key, item in value.items() if key != "items"}
        return summary, list(bounded["items"])
    return {}, [value]


class PreparedObservationService:
    """Serve cached facts and render extra views only from retained immutable evidence."""

    def __init__(self, artifact_store: PreparedArtifactStore, *, renderer: Renderer | None):
        self._store = artifact_store
        self._renderer = renderer
        self._lock = threading.RLock()
        self._render_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._supplemental_counts: dict[str, int] = {}

    def _load_cache(self, prepare_id: str) -> dict[str, Any]:
        try:
            path = self._store.observation_cache_path(prepare_id)
            value = json.loads(path.read_text(encoding="utf-8"))
        except PreparedArtifactError as exc:
            raise _from_artifact_error(exc) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_CACHE_INVALID", "observation cache cannot be read"
            ) from exc
        if not isinstance(value, dict) or value.get("prepareId") != prepare_id:
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_CACHE_INVALID", "observation cache identity does not match prepare"
            )
        sections = value.get("sections")
        if not isinstance(sections, dict):
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_CACHE_INVALID", "observation cache has no sections"
            )
        return value

    def inspect(
        self,
        prepare_id: str,
        section: str,
        *,
        cursor: str | None = None,
        limit: int = _DEFAULT_PAGE_LIMIT,
    ) -> dict[str, Any]:
        if section not in SUPPORTED_SECTIONS:
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_INVALID_SECTION",
                f"section must be one of {', '.join(SUPPORTED_SECTIONS)}",
            )
        offset = _parse_cursor(cursor)
        bounded_limit = _validate_limit(limit)
        try:
            with self._store.lease(prepare_id):
                cache = self._load_cache(prepare_id)
                evidence = self._store.evidence_identity(prepare_id)
        except PreparedArtifactError as exc:
            raise _from_artifact_error(exc) from exc

        raw_section = cache["sections"].get(section)
        if not isinstance(raw_section, dict):
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_CACHE_INVALID", f"cache is missing {section} section"
            )
        summary, details = _section_details(section, raw_section)
        page = details[offset : offset + bounded_limit]
        next_offset = offset + len(page)
        next_cursor = str(next_offset) if next_offset < len(details) else None
        result: dict[str, Any] = {
            "prepareId": prepare_id,
            "section": section,
            "boundedDetails": {
                "summary": summary,
                "totalCount": len(details),
                "items": page,
            },
            "nextCursor": next_cursor,
            "evidenceIdentity": evidence,
        }
        item_key = cache.get("itemKey")
        if isinstance(item_key, str) and item_key:
            result["itemKey"] = item_key
        return result

    async def render(
        self,
        prepare_id: str,
        view: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if view not in SUPPORTED_VIEWS:
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_INVALID_VIEW",
                f"view must be one of {', '.join(SUPPORTED_VIEWS)}",
            )
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_INVALID_IDEMPOTENCY_KEY",
                "idempotency_key must be a non-empty string",
            )
        cache_key = (prepare_id, view, idempotency_key)
        with self._lock:
            cached = self._render_cache.get(cache_key)
        if cached is not None:
            try:
                artifact_id = cached["supplementalArtifact"]["artifactId"]
                self._store.resolve(artifact_id)
                self._store.evidence_identity(prepare_id)
            except PreparedArtifactError as exc:
                raise _from_artifact_error(exc) from exc
            return cached
        try:
            self._store.evidence_identity(prepare_id)
        except PreparedArtifactError as exc:
            raise _from_artifact_error(exc) from exc
        if self._renderer is None:
            raise PreparedObservationError(
                "PREPARED_OBSERVATION_RENDERER_UNAVAILABLE",
                "supplemental view renderer is unavailable",
            )

        digest = hashlib.sha256(
            f"{prepare_id}\0{view}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:24]
        try:
            with self._store.lease(prepare_id) as workspace:
                cache = self._load_cache(prepare_id)
                evidence = self._store.evidence_identity(prepare_id)
                source = self._store.retained_source_path(prepare_id)
                output = workspace / f"supplemental-{view.lower()}-{digest}.png"
                try:
                    timings = await self._renderer(
                        source_path=source,
                        output_path=output,
                        view=view,
                        prepare_id=prepare_id,
                    )
                except BaseException:
                    try:
                        output.unlink()
                    except FileNotFoundError:
                        pass
                    raise
                if not output.is_file():
                    raise PreparedObservationError(
                        "PREPARED_OBSERVATION_RENDER_FAILED",
                        "supplemental renderer completed without producing a PNG",
                    )
                ref = self._store.register_artifact(
                    prepare_id,
                    "PREVIEW",
                    output,
                    content_type="image/png",
                )
                public_ref = _public_artifact(ref, output)
        except PreparedArtifactError as exc:
            raise _from_artifact_error(exc) from exc

        with self._lock:
            existing = self._render_cache.get(cache_key)
            if existing is not None:
                return existing
            count = self._supplemental_counts.get(prepare_id, 0) + 1
            self._supplemental_counts[prepare_id] = count
            result: dict[str, Any] = {
                "prepareId": prepare_id,
                "view": view,
                "supplementalArtifact": public_ref,
                "evidenceIdentity": evidence,
                "timings": timings if isinstance(timings, dict) else {},
                "supplementalRequestCount": count,
            }
            item_key = cache.get("itemKey")
            if isinstance(item_key, str) and item_key:
                result["itemKey"] = item_key
            self._render_cache[cache_key] = result
            return result

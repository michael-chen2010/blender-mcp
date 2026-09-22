# blender_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context, Image
from mcp.types import CallToolResult, TextContent
import socket
import json
import asyncio
import logging
import tempfile
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List
import os
import sys
import time
from pathlib import Path
import base64
from urllib.parse import urlparse

# Import telemetry
from .telemetry import record_startup, get_telemetry, EventType
from .telemetry_decorator import telemetry_tool, trajectory_tool
from .addon_manager import (
    handshake_addon,
    format_handshake_log,
    run_cli as run_addon_cli,
    EXPECTED_ADDON_PROTOCOL_VERSION,
    check_addon_status_on_startup,
)
from .consent_prompt import maybe_prompt_for_consent
from .safe_mode import safe_mode_enabled, validate_code, SandboxViolation, SAFE_MODE_ENV
from .blender_runtime import (
    ASSET_HARD_CAP,
    SUPPORTED_ASSET_PROFILES,
    configured_asset_concurrency,
    recommended_asset_concurrency,
    resolve_blender_runtime,
)
from .asset_pipeline import (
    AssetBatchError,
    AssetPipelineManager,
    AssetPrepareError,
    asset_worker_slot,
    discover_blend_files as discover_local_blend_files,
    get_prepared_artifact_store,
    prepare_blend_file,
    render_supplemental_view,
)
from .prepared_artifacts import PreparedArtifactError
from .prepared_observation import PreparedObservationError, PreparedObservationService
from .file_transfer import FileTransferError, PreparedArtifactTransferService

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BlenderMCPServer")

# Default configuration
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876

_addon_handshake = None
_addon_handshake_checked = False
_addon_handshake_lock = threading.Lock()
_prepared_observation_service: PreparedObservationService | None = None
_prepared_observation_service_lock = threading.Lock()
_prepared_artifact_transfer_service: PreparedArtifactTransferService | None = None
_prepared_artifact_transfer_service_lock = threading.Lock()
_asset_pipeline_manager: AssetPipelineManager | None = None
_asset_pipeline_manager_lock = threading.Lock()

@dataclass
class BlenderConnection:
    host: str
    port: int
    sock: socket.socket = None  # Changed from 'socket' to 'sock' to avoid naming conflict
    # Serializes send+receive so two commands can never interleave on one socket.
    # Without this, a second command's response can be read as the first's, and
    # the stream stays desynced until the 180s timeout fires.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def connect(self) -> bool:
        """Connect to the Blender addon socket server"""
        if self.sock:
            return True
            
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Blender at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Blender: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Blender addon"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Blender: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        # Use a consistent timeout value that matches the addon's timeout
        sock.settimeout(180.0)  # Match the addon's timeout
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        # If we get an empty chunk, the connection might be closed
                        if not chunks:  # If we haven't received anything yet, this is an error
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        # If we get here, it parsed successfully
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    # If we hit a timeout during receiving, break the loop and try to use what we have
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise  # Re-raise to be handled by the caller
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        # Try to use what we have
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                # Try to parse what we have
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                # If we can't parse it, it's incomplete
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Blender and return the response"""
        # Hold the lock across send+receive: the response is matched to the
        # command purely by ordering on the stream, so overlapping calls would
        # hand each other's responses back.
        with self._lock:
            return self._send_command_locked(command_type, params)

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Blender")

        command = {
            "type": command_type,
            "params": params or {}
        }

        try:
            # Log the command being sent
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set a timeout for receiving - use the same timeout as in receive_full_response
            self.sock.settimeout(180.0)  # Match the addon's timeout
            
            # Receive the response using the improved receive_full_response method
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")
            
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")
            
            if response.get("status") == "error":
                logger.error(f"Blender error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Blender"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Blender")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            # Just invalidate the current socket so it will be recreated next time
            self.sock = None
            raise Exception("Timeout waiting for Blender response - try simplifying your request. If Blender is running headless (blender -b), commands never execute; run Blender with a GUI or via 'xvfb-run -a blender' instead")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Blender lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Blender: {str(e)}")
            # Try to log what was received
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            raise Exception(f"Invalid response from Blender: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Blender: {str(e)}")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            self.sock = None
            raise Exception(f"Communication error with Blender: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    # We don't need to create a connection here since we're using the global connection
    # for resources and tools

    try:
        # Just log that we're starting up
        logger.info("BlenderMCP server starting up")

        try:
            status = check_addon_status_on_startup()
            if status.needs_action:
                logger.warning(status.message)
            elif status.message:
                logger.info(status.message)
        except Exception as e:
            logger.debug(f"Addon status check skipped: {e}")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        # Try to connect to Blender on startup to verify it's available
        try:
            # This will initialize the global connection if needed
            blender = get_blender_connection()
            logger.info("Successfully connected to Blender on startup")
            if _addon_handshake and not _addon_handshake.up_to_date:
                logger.warning(format_handshake_log(_addon_handshake))
        except Exception as e:
            logger.warning(f"Could not connect to Blender on startup: {str(e)}")
            logger.warning("Make sure the Blender addon is running before using Blender resources or tools")

        # Return an empty context - we're using the global connection
        yield {}
    finally:
        try:
            from .trajectory import get_trajectory_recorder

            recorder = get_trajectory_recorder()
            recorder.close_episode("session_end")
            recorder.flush(2.0)
        except Exception as e:
            logger.debug(f"Episode close on shutdown skipped: {e}")
        if _asset_pipeline_manager is not None:
            try:
                await _asset_pipeline_manager.shutdown()
            except Exception as e:
                logger.debug(f"Asset pipeline shutdown cleanup skipped: {e}")
        # Clean up the global connection on shutdown
        global _blender_connection
        if _blender_connection:
            logger.info("Disconnecting from Blender on shutdown")
            _blender_connection.disconnect()
            _blender_connection = None
        logger.info("BlenderMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "BlenderMCP",
    lifespan=server_lifespan
)

# Resource endpoints

# Global connection for resources (since resources can't access context)
_blender_connection = None

def _maybe_handshake_addon(blender: BlenderConnection) -> None:
    """Run addon version handshake once per process after a live connection."""
    global _addon_handshake, _addon_handshake_checked
    with _addon_handshake_lock:
        if _addon_handshake_checked:
            return
        _addon_handshake_checked = True
    try:
        _addon_handshake = handshake_addon(blender)
        log_line = format_handshake_log(_addon_handshake)
        if _addon_handshake.up_to_date:
            logger.info(log_line)
        else:
            logger.warning(log_line)
    except Exception as e:
        logger.debug(f"Addon handshake skipped: {e}")


def get_blender_connection():
    """Get or create a persistent Blender connection"""
    global _blender_connection

    # Reuse the existing connection. We deliberately do NOT probe it with a
    # command here: that put two commands on the wire for every tool call, and
    # any overlap desynced the response stream until the socket timeout fired.
    # A dead socket is detected by the next real command and reconnected then.
    if _blender_connection is not None and _blender_connection.sock is not None:
        return _blender_connection

    # Create a new connection if needed
    if _blender_connection is None:
        host = os.getenv("BLENDER_HOST", DEFAULT_HOST)
        port = int(os.getenv("BLENDER_PORT", DEFAULT_PORT))
        _blender_connection = BlenderConnection(host=host, port=port)
        if not _blender_connection.connect():
            logger.error("Failed to connect to Blender")
            _blender_connection = None
            raise Exception("Could not connect to Blender. Make sure the Blender addon is running.")
        logger.info("Created new persistent connection to Blender")
        _maybe_handshake_addon(_blender_connection)

    return _blender_connection


def create_blend_snapshot(
    filepath: str,
    *,
    closure_mode: str = "ASSET_CLOSURE",
) -> Dict[str, Any]:
    """Ask the GUI Add-on to snapshot CURRENT_SELECTION on Blender's main thread."""
    valid_modes = {"SELECTED_ONLY", "INCLUDE_DESCENDANTS", "ASSET_CLOSURE"}
    if not isinstance(filepath, str) or not filepath:
        raise ValueError("filepath must be a non-empty string")
    if closure_mode not in valid_modes:
        raise ValueError(
            f"Unsupported closure_mode {closure_mode!r}; expected one of {sorted(valid_modes)}"
        )

    blender = get_blender_connection()
    with _addon_handshake_lock:
        handshake = _addon_handshake
    if (
        handshake is not None
        and handshake.source == "native"
        and "create_blend_snapshot" not in handshake.capabilities
    ):
        raise RuntimeError(
            "Connected Blender Add-on does not advertise create_blend_snapshot. "
            "Run `uvx blender-mcp install-addon`, then restart Blender or re-enable the Add-on."
        )

    return blender.send_command(
        "create_blend_snapshot",
        {
            "filepath": filepath,
            "selectionMode": "CURRENT_SELECTION",
            "closureMode": closure_mode,
        },
    )


@mcp.tool()
async def get_addon_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check whether the connected Blender addon matches this MCP server version.

    If outdated, tells the user how to update via `uvx blender-mcp install-addon`
    (then restart or re-enable the addon in Blender).

    `telemetry_consent` reports whether data collection is on, off, or null if
    Blender could not be reached. Use it to answer telemetry status questions.
    """
    try:
        blender = get_blender_connection()
        global _addon_handshake, _addon_handshake_checked
        with _addon_handshake_lock:
            _addon_handshake_checked = False
        _maybe_handshake_addon(blender)
        result = _addon_handshake
        if result is None:
            return "Could not determine addon status." + await maybe_prompt_for_consent(ctx)
        payload = {
            "up_to_date": result.up_to_date,
            "protocol_version": result.protocol_version,
            "expected_protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
            "addon_version": result.addon_version,
            "capabilities": result.capabilities,
            "blender_version": result.blender_version,
            "source": result.source,
            "warning": result.warning,
            "telemetry_consent": get_telemetry().check_user_consent(),
            "update_command": "uvx blender-mcp install-addon",
            "after_install": (
                "If the addon file was updated: in Blender, Preferences → Add-ons → "
                "disable/enable 'Interface: Blender MCP', or restart Blender, then Start MCP Server."
            ),
        }
        return json.dumps(payload, indent=2) + await maybe_prompt_for_consent(ctx)
    except Exception as e:
        return f"Error checking addon status: {e}"


def _resolve_server_asset_runtime():
    """Resolve background Blender using the server's current local Add-on handshake."""

    with _addon_handshake_lock:
        handshake = _addon_handshake
    addon_binary_path = handshake.blender_binary_path if handshake is not None else None
    try:
        return resolve_blender_runtime(
            addon_binary_path=addon_binary_path,
            blender_host=os.getenv("BLENDER_HOST", DEFAULT_HOST),
        )
    except FileNotFoundError:
        return None


@mcp.tool()
def get_asset_pipeline_status() -> str:
    """Report local background asset-pipeline capability without touching Blender GUI."""
    cpu_count = os.cpu_count()
    with _addon_handshake_lock:
        handshake = _addon_handshake
    runtime = _resolve_server_asset_runtime()

    recommended = recommended_asset_concurrency(cpu_count)
    configured = configured_asset_concurrency(cpu_count)
    blender_version = None
    if runtime is not None and runtime.source == "LOCAL_ADDON" and handshake is not None:
        blender_version = handshake.blender_version

    return json.dumps(
        {
            "available": runtime is not None,
            "source": runtime.source if runtime is not None else None,
            "blenderVersion": blender_version,
            "supportedProfiles": list(SUPPORTED_ASSET_PROFILES),
            "cpuCount": cpu_count,
            "recommendedConcurrency": recommended,
            "configuredMaxConcurrency": configured,
            "hardCap": ASSET_HARD_CAP,
        },
        indent=2,
    )


class _PrepareBlendAssetInputError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _prepare_blend_asset_error(code: str, message: str) -> CallToolResult:
    text = f"{code}: {message}"
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"error": {"code": code, "message": message}},
        isError=True,
    )


def _stable_prepare_error(exc: Exception, default_code: str) -> CallToolResult:
    message = str(exc)
    prefix, separator, detail = message.partition(":")
    if (
        separator
        and prefix
        and prefix == prefix.upper()
        and all(char.isalnum() or char == "_" for char in prefix)
    ):
        return _prepare_blend_asset_error(prefix, detail.strip() or message)
    return _prepare_blend_asset_error(default_code, message)


async def _render_prepared_supplemental_view(**kwargs):
    return await render_supplemental_view(
        runtime=_resolve_server_asset_runtime(),
        **kwargs,
    )


def get_prepared_observation_service() -> PreparedObservationService:
    """Return the process-wide prepared observation service used across MCP calls."""

    global _prepared_observation_service
    if _prepared_observation_service is None:
        with _prepared_observation_service_lock:
            if _prepared_observation_service is None:
                _prepared_observation_service = PreparedObservationService(
                    get_prepared_artifact_store(), renderer=_render_prepared_supplemental_view
                )
    return _prepared_observation_service


def get_asset_pipeline_manager() -> AssetPipelineManager:
    """Return the process-wide batch prepare registry and scheduler."""

    global _asset_pipeline_manager
    if _asset_pipeline_manager is None:
        with _asset_pipeline_manager_lock:
            if _asset_pipeline_manager is None:
                _asset_pipeline_manager = AssetPipelineManager(
                    artifact_store=get_prepared_artifact_store(),
                    runtime_provider=_resolve_server_asset_runtime,
                )
    return _asset_pipeline_manager


def get_prepared_artifact_transfer_service() -> PreparedArtifactTransferService:
    """Return the process-wide prepared artifact transfer service used across MCP calls."""

    global _prepared_artifact_transfer_service
    if _prepared_artifact_transfer_service is None:
        with _prepared_artifact_transfer_service_lock:
            if _prepared_artifact_transfer_service is None:
                _prepared_artifact_transfer_service = PreparedArtifactTransferService(
                    get_prepared_artifact_store(),
                    prepare_manifest_resolver=get_asset_pipeline_manager().resolve_upload_manifest,
                )
    return _prepared_artifact_transfer_service


def _validate_prepare_source(source: Dict[str, Any]) -> tuple[str, Path | None]:
    if not isinstance(source, dict):
        raise _PrepareBlendAssetInputError(
            "PREPARE_INVALID_SOURCE", "source must be an object"
        )

    kind = source.get("kind")
    if kind == "BLEND_FILE":
        if set(source) - {"kind", "path"}:
            raise _PrepareBlendAssetInputError(
                "PREPARE_INVALID_SOURCE", "BLEND_FILE source contains unsupported fields"
            )
        raw_path = source.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise _PrepareBlendAssetInputError(
                "PREPARE_INVALID_SOURCE", "BLEND_FILE source.path is required"
            )
        path = Path(raw_path)
        if path.suffix.lower() != ".blend":
            raise _PrepareBlendAssetInputError(
                "PREPARE_INVALID_SOURCE", "BLEND_FILE source.path must end with .blend"
            )
        if not path.is_file():
            raise _PrepareBlendAssetInputError(
                "BLEND_SOURCE_NOT_FOUND", f"Blend source does not exist: {path}"
            )
        return kind, path

    if kind == "CURRENT_SELECTION":
        if set(source) != {"kind"}:
            raise _PrepareBlendAssetInputError(
                "PREPARE_INVALID_SOURCE", "CURRENT_SELECTION accepts only the kind field"
            )
        return kind, None

    raise _PrepareBlendAssetInputError(
        "PREPARE_INVALID_SOURCE",
        "source.kind must be BLEND_FILE or CURRENT_SELECTION",
    )


def _validate_prepare_overrides(overrides: Dict[str, Any] | None) -> None:
    if overrides is None or overrides == {}:
        return
    if not isinstance(overrides, dict):
        raise _PrepareBlendAssetInputError(
            "PREPARE_INVALID_OVERRIDES", "overrides must be an object"
        )

    allowed = {"preview", "includeAnimationDetails", "preparePayload", "runValidation"}
    unknown = set(overrides) - allowed
    if unknown:
        raise _PrepareBlendAssetInputError(
            "PREPARE_INVALID_OVERRIDES",
            f"unsupported override field(s): {', '.join(sorted(unknown))}",
        )

    for key in ("includeAnimationDetails", "preparePayload", "runValidation"):
        if key in overrides and not isinstance(overrides[key], bool):
            raise _PrepareBlendAssetInputError(
                "PREPARE_INVALID_OVERRIDES", f"overrides.{key} must be boolean"
            )

    preview = overrides.get("preview")
    if preview is not None:
        if not isinstance(preview, dict):
            raise _PrepareBlendAssetInputError(
                "PREPARE_INVALID_OVERRIDES", "overrides.preview must be an object"
            )
        preview_unknown = set(preview) - {"width", "height", "views"}
        if preview_unknown:
            raise _PrepareBlendAssetInputError(
                "PREPARE_INVALID_OVERRIDES",
                "unsupported preview override field(s): "
                + ", ".join(sorted(preview_unknown)),
            )
        for dimension in ("width", "height"):
            if dimension in preview:
                value = preview[dimension]
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise _PrepareBlendAssetInputError(
                        "PREPARE_INVALID_OVERRIDES",
                        f"overrides.preview.{dimension} must be a positive integer",
                    )
        if "views" in preview:
            views = preview["views"]
            supported_views = {"MAIN", "FRONT", "BACK", "LEFT", "RIGHT", "TOP"}
            if (
                not isinstance(views, list)
                or not views
                or any(not isinstance(view, str) or view not in supported_views for view in views)
            ):
                raise _PrepareBlendAssetInputError(
                    "PREPARE_INVALID_OVERRIDES",
                    "overrides.preview.views must contain supported view names",
                )

    # The current worker has deterministic profile presets but does not yet consume
    # per-request overrides. Rejecting non-empty values is safer than silently
    # accepting settings that would have no effect. Supplemental views are Task 5a.
    raise _PrepareBlendAssetInputError(
        "PREPARE_OVERRIDE_UNSUPPORTED",
        "non-empty overrides are not supported by the current prepare worker",
    )


def _prepare_blend_asset_content(result: Dict[str, Any], profile: str) -> CallToolResult:
    prepare_id = str(result.get("prepareId") or "")
    observation = result.get("observation")
    timings = result.get("timings")
    raw_artifacts = result.get("artifacts")
    if not prepare_id or not isinstance(observation, dict) or not isinstance(timings, dict):
        raise AssetPrepareError(
            "BLENDER_PREPARE_RESULT_INVALID: worker result is missing prepareId/observation/timings"
        )
    if not isinstance(raw_artifacts, list):
        raise AssetPrepareError(
            "BLENDER_PREPARE_RESULT_INVALID: worker result is missing artifact references"
        )

    store = get_prepared_artifact_store()
    artifacts = []
    main_preview = None
    for raw in raw_artifacts:
        if not isinstance(raw, dict):
            raise AssetPrepareError(
                "BLENDER_PREPARE_RESULT_INVALID: artifact reference must be an object"
            )
        artifact_id = raw.get("artifact_id")
        kind = raw.get("kind")
        if not isinstance(artifact_id, str) or not artifact_id or not isinstance(kind, str):
            raise AssetPrepareError(
                "BLENDER_PREPARE_RESULT_INVALID: artifact reference is missing id/kind"
            )
        path = store.resolve(artifact_id)
        public_ref = {
            "artifactId": artifact_id,
            "kind": kind,
            "fileName": path.name,
            "size": raw.get("size"),
            "sha256": raw.get("sha256"),
            "mimeType": raw.get("content_type"),
            "expiresAt": raw.get("expires_at"),
        }
        artifacts.append(public_ref)
        if kind == "PREVIEW" and main_preview is None:
            main_preview = path

    if main_preview is None:
        raise AssetPrepareError(
            "BLENDER_PREPARE_ARTIFACT_MISSING: MAIN preview artifact is missing"
        )

    structured: Dict[str, Any] = {
        "prepareId": prepare_id,
        "observation": observation,
        "artifacts": artifacts,
        "warnings": result.get("warnings") if isinstance(result.get("warnings"), list) else [],
        "timings": timings,
    }
    if isinstance(result.get("validation"), dict):
        structured["validation"] = result["validation"]

    object_count = observation.get("structure", {}).get("objectCount")
    object_summary = f", {object_count} object(s)" if isinstance(object_count, int) else ""
    summary = (
        f"Prepared Blender asset {prepare_id} with profile {profile}{object_summary}; "
        f"{len(artifacts)} prepared artifact(s)."
    )
    return CallToolResult(
        content=[
            TextContent(type="text", text=summary),
            Image(path=main_preview, format="png").to_image_content(),
        ],
        structuredContent=structured,
        isError=False,
    )


@mcp.tool()
async def prepare_blend_asset(
    source: Dict[str, Any],
    profile: str,
    overrides: Dict[str, Any] | None = None,
) -> CallToolResult:
    """Prepare one .blend or the current Blender selection in an isolated worker.

    BLEND_FILE never touches the GUI socket. CURRENT_SELECTION performs one
    ASSET_CLOSURE snapshot handoff, then uses the same background pipeline.
    The result carries structured observation/artifact data plus the MAIN image.
    """
    try:
        source_kind, source_path = _validate_prepare_source(source)
        if profile not in SUPPORTED_ASSET_PROFILES:
            raise _PrepareBlendAssetInputError(
                "PREPARE_INVALID_PROFILE",
                f"profile must be one of {', '.join(SUPPORTED_ASSET_PROFILES)}",
            )
        _validate_prepare_overrides(overrides)
        runtime = _resolve_server_asset_runtime()

        if source_kind == "BLEND_FILE":
            async with asset_worker_slot():
                prepared = await asyncio.to_thread(
                    prepare_blend_file,
                    source_path,
                    profile=profile,
                    source_kind="BLEND_FILE",
                    runtime=runtime,
                )
            return _prepare_blend_asset_content(prepared, profile)

        with tempfile.TemporaryDirectory(prefix="blendermcp-selection-") as snapshot_dir:
            snapshot_path = Path(snapshot_dir) / "current-selection.blend"
            try:
                create_blend_snapshot(
                    str(snapshot_path), closure_mode="ASSET_CLOSURE"
                )
            except Exception as exc:
                return _stable_prepare_error(exc, "CURRENT_SELECTION_SNAPSHOT_FAILED")
            if not snapshot_path.is_file():
                return _prepare_blend_asset_error(
                    "CURRENT_SELECTION_SHARED_FILESYSTEM_REQUIRED",
                    "selection snapshot is not visible to the Blender MCP server filesystem",
                )
            async with asset_worker_slot():
                prepared = await asyncio.to_thread(
                    prepare_blend_file,
                    snapshot_path,
                    profile=profile,
                    source_kind="CURRENT_SELECTION",
                    runtime=runtime,
                )
            return _prepare_blend_asset_content(prepared, profile)
    except _PrepareBlendAssetInputError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except AssetPrepareError as exc:
        return _stable_prepare_error(exc, "BLENDER_PREPARE_FAILED")
    except FileNotFoundError as exc:
        return _stable_prepare_error(exc, "BLEND_SOURCE_NOT_FOUND")
    except ValueError as exc:
        return _stable_prepare_error(exc, "BLENDER_PREPARE_INVALID")
    except Exception as exc:
        logger.exception("prepare_blend_asset failed")
        return _stable_prepare_error(exc, "BLENDER_PREPARE_FAILED")


_PREPARED_ASSET_WINDOW_MAX = 16
_COMPACT_NAME_LIMIT = 12


def _bounded_string_values(value: Any, *, limit: int = _COMPACT_NAME_LIMIT) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item][:limit]


def _compact_prepared_observation(observation: Any) -> Dict[str, Any]:
    if not isinstance(observation, dict):
        return {}
    source = observation.get("source") if isinstance(observation.get("source"), dict) else {}
    structure = observation.get("structure") if isinstance(observation.get("structure"), dict) else {}
    geometry = observation.get("geometry") if isinstance(observation.get("geometry"), dict) else {}
    materials = observation.get("materials") if isinstance(observation.get("materials"), dict) else {}
    deformation = observation.get("deformation") if isinstance(observation.get("deformation"), dict) else {}
    evidence = observation.get("evidenceSummary") if isinstance(observation.get("evidenceSummary"), dict) else {}
    preview_evidence = observation.get("previewEvidence") if isinstance(observation.get("previewEvidence"), dict) else {}
    payload_evidence = observation.get("payloadEvidence") if isinstance(observation.get("payloadEvidence"), dict) else {}

    compact: Dict[str, Any] = {}
    if observation.get("observationScope") is not None:
        compact["observationScope"] = observation.get("observationScope")
    compact_source = {
        key: source[key]
        for key in ("kind", "displayName", "sourceSize", "sourceSha256", "blenderVersion")
        if key in source
    }
    if compact_source:
        compact["source"] = compact_source
    scalar_fields = (
        ("objectCount", structure.get("objectCount")),
        ("vertexCount", geometry.get("vertexCount")),
        ("triangleCount", geometry.get("triangleCount")),
        ("hasUv", geometry.get("hasUv")),
        ("lodCount", geometry.get("lodCount")),
        ("materialCount", materials.get("materialCount")),
        ("materialWorkflow", materials.get("materialWorkflow")),
        ("rigged", deformation.get("rigged")),
        ("animated", deformation.get("animated")),
        ("armatureCount", deformation.get("armatureCount")),
        ("actionCount", deformation.get("actionCount")),
        ("shapeKeyCount", deformation.get("shapeKeyCount")),
    )
    for key, value in scalar_fields:
        if value is not None:
            compact[key] = value

    dimensions = geometry.get("dimensionsMeters")
    if isinstance(dimensions, dict):
        compact["dimensionsMeters"] = {
            axis: dimensions[axis]
            for axis in ("x", "y", "z")
            if axis in dimensions
        }
    pbr_channels = materials.get("pbrChannels")
    if isinstance(pbr_channels, list):
        compact["pbrChannels"] = [
            value for value in pbr_channels if isinstance(value, str)
        ][:16]
    compact["primaryObjectNames"] = _bounded_string_values(evidence.get("objectNames"))
    compact["materialNames"] = _bounded_string_values(evidence.get("materialNames"))

    preview_main = preview_evidence.get("main") if isinstance(preview_evidence.get("main"), dict) else {}
    main_preview_evidence = {
        key: preview_main[key]
        for key in ("width", "height", "view")
        if key in preview_main
    }
    if preview_evidence.get("presetVersion") is not None:
        main_preview_evidence["presetVersion"] = preview_evidence.get("presetVersion")
    if main_preview_evidence:
        compact["mainPreviewEvidence"] = main_preview_evidence

    compact_payload_evidence = {
        key: payload_evidence[key]
        for key in (
            "format",
            "compression",
            "generatedBlenderVersion",
            "sha256",
            "size",
            "factsVerifiedAfterReopen",
        )
        if key in payload_evidence
    }
    if compact_payload_evidence:
        compact["payloadEvidence"] = compact_payload_evidence
    return compact


def _public_prepared_artifact(raw: Any) -> Dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    artifact_id = raw.get("artifact_id") or raw.get("artifactId")
    kind = raw.get("kind")
    if not isinstance(artifact_id, str) or not isinstance(kind, str):
        return None
    return {
        "artifactId": artifact_id,
        "kind": kind,
        "size": raw.get("size"),
        "sha256": raw.get("sha256"),
        "mimeType": raw.get("content_type") or raw.get("mimeType"),
        "expiresAt": raw.get("expires_at") or raw.get("expiresAt"),
    }


def _prepared_window_item_content(raw_item: Dict[str, Any]) -> tuple[Dict[str, Any], TextContent, Any | None]:
    started = time.perf_counter()
    item_key = str(raw_item.get("itemKey") or "")
    source_display_name = str(raw_item.get("sourceDisplayName") or "")
    source_fingerprint = raw_item.get("sourceFingerprint")
    result = raw_item.get("result") if isinstance(raw_item.get("result"), dict) else {}
    prepare_id = result.get("prepareId")

    if not isinstance(prepare_id, str) or not prepare_id:
        structured = {
            "itemKey": item_key,
            "sourceDisplayName": source_display_name,
            "status": "FAILED",
            "error": {
                "code": "BATCH_PREPARE_WINDOW_RESULT_INVALID",
                "message": "READY item has no prepareId",
            },
        }
        return structured, TextContent(type="text", text=f"{item_key} — {source_display_name}: unavailable"), None

    try:
        store = get_prepared_artifact_store()
        preview_identity: Dict[str, Any] | None = None
        payload_identity: Dict[str, Any] | None = None
        preview_path: Path | None = None
        with store.lease(prepare_id):
            for raw_artifact in result.get("artifacts", []):
                public_artifact = _public_prepared_artifact(raw_artifact)
                if public_artifact is None:
                    continue
                if public_artifact["kind"] == "PREVIEW" and preview_identity is None:
                    preview_identity = public_artifact
                    preview_path = store.resolve(public_artifact["artifactId"])
                elif public_artifact["kind"] == "PAYLOAD" and payload_identity is None:
                    payload_identity = public_artifact

            if preview_identity is None or preview_path is None:
                raise PreparedArtifactError(
                    "PREPARED_WINDOW_MAIN_PREVIEW_MISSING",
                    "READY item has no registered MAIN preview",
                )
            mime_type = preview_identity.get("mimeType")
            image_format = "jpeg" if mime_type == "image/jpeg" else "png"
            image_content = Image(path=preview_path, format=image_format).to_image_content()

        observation = result.get("observation") if isinstance(result.get("observation"), dict) else {}
        evidence_identity: Dict[str, Any] = {"prepareId": prepare_id}
        if isinstance(source_fingerprint, str):
            evidence_identity["sourceFingerprint"] = source_fingerprint
        if observation.get("analyzerVersion") is not None:
            evidence_identity["analyzerVersion"] = observation.get("analyzerVersion")
        if observation.get("schemaVersion") is not None:
            evidence_identity["observationSchemaVersion"] = observation.get("schemaVersion")

        prepared_artifacts: Dict[str, Any] = {"mainPreview": preview_identity}
        if payload_identity is not None:
            prepared_artifacts["payload"] = payload_identity

        structured = {
            "itemKey": item_key,
            "prepareId": prepare_id,
            "sourceDisplayName": source_display_name,
            "status": "READY",
            "evidenceIdentity": evidence_identity,
            "compactObservation": _compact_prepared_observation(observation),
            "preparedArtifacts": prepared_artifacts,
            "validation": result.get("validation") if isinstance(result.get("validation"), dict) else {},
            "timings": {
                "cacheReadMs": round((time.perf_counter() - started) * 1000.0, 3),
            },
        }
        label = TextContent(type="text", text=f"{item_key} — {source_display_name}")
        return structured, label, image_content
    except PreparedArtifactError as exc:
        structured = {
            "itemKey": item_key,
            "prepareId": prepare_id,
            "sourceDisplayName": source_display_name,
            "status": "FAILED",
            "error": {
                "code": exc.code,
                "message": str(exc).partition(":")[2].strip() or str(exc),
            },
            "timings": {
                "cacheReadMs": round((time.perf_counter() - started) * 1000.0, 3),
            },
        }
        label = TextContent(type="text", text=f"{item_key} — {source_display_name}: {exc.code}")
        return structured, label, None
    except (FileNotFoundError, OSError) as exc:
        structured = {
            "itemKey": item_key,
            "prepareId": prepare_id,
            "sourceDisplayName": source_display_name,
            "status": "FAILED",
            "error": {
                "code": "PREPARED_WINDOW_PREVIEW_UNAVAILABLE",
                "message": str(exc) or "MAIN preview cannot be read",
            },
            "timings": {
                "cacheReadMs": round((time.perf_counter() - started) * 1000.0, 3),
            },
        }
        label = TextContent(type="text", text=f"{item_key} — {source_display_name}: preview unavailable")
        return structured, label, None


def _batch_prepare_page_content(page: Dict[str, Any]) -> CallToolResult:
    """Convert an internal batch page into bounded model-visible content."""

    mode = page.get("mode") or "LEGACY_FULL"
    public_page: Dict[str, Any] = {
        "batchPrepareId": page.get("batchPrepareId"),
        "status": page.get("status"),
        "mode": mode,
        "createdAt": page.get("createdAt"),
        "startedAt": page.get("startedAt"),
        "completedAt": page.get("completedAt"),
        "counts": page.get("counts", {}),
        "items": [],
        "nextCursor": page.get("nextCursor"),
    }
    content = [
        TextContent(
            type="text",
            text=f"Batch prepare {page.get('batchPrepareId')} is {page.get('status')}.",
        )
    ]
    raw_items = page.get("items") if isinstance(page.get("items"), list) else []
    if mode == "STATUS":
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            public_page["items"].append(
                {
                    key: raw_item[key]
                    for key in (
                        "itemKey",
                        "sourceDisplayName",
                        "sourceFingerprint",
                        "status",
                        "attempts",
                        "prepareId",
                        "queuedAt",
                        "startedAt",
                        "completedAt",
                        "timings",
                        "error",
                    )
                    if key in raw_item
                }
            )
        return CallToolResult(content=content, structuredContent=public_page, isError=False)

    store = get_prepared_artifact_store()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        public_item: Dict[str, Any] = {
            key: raw_item[key]
            for key in ("itemKey", "sourceDisplayName", "sourceFingerprint", "status", "attempts", "error")
            if key in raw_item
        }
        raw_result = raw_item.get("result")
        preview_path = None
        if isinstance(raw_result, dict):
            public_result: Dict[str, Any] = {
                key: raw_result[key]
                for key in ("prepareId", "profile", "observation", "warnings", "timings", "validation")
                if key in raw_result
            }
            public_artifacts = []
            raw_artifacts = raw_result.get("artifacts")
            if isinstance(raw_artifacts, list):
                for raw_artifact in raw_artifacts:
                    if not isinstance(raw_artifact, dict):
                        continue
                    artifact_id = raw_artifact.get("artifact_id") or raw_artifact.get("artifactId")
                    kind = raw_artifact.get("kind")
                    if not isinstance(artifact_id, str) or not isinstance(kind, str):
                        continue
                    path = store.resolve(artifact_id)
                    public_artifacts.append(
                        {
                            "artifactId": artifact_id,
                            "kind": kind,
                            "fileName": path.name,
                            "size": raw_artifact.get("size"),
                            "sha256": raw_artifact.get("sha256"),
                            "mimeType": raw_artifact.get("content_type") or raw_artifact.get("mimeType"),
                            "expiresAt": raw_artifact.get("expires_at") or raw_artifact.get("expiresAt"),
                        }
                    )
                    if kind == "PREVIEW" and preview_path is None:
                        preview_path = path
            public_result["artifacts"] = public_artifacts
            public_item["result"] = public_result
        public_page["items"].append(public_item)
        if public_item.get("status") == "READY" and preview_path is not None:
            label = f"{public_item.get('itemKey')} — {public_item.get('sourceDisplayName')}"
            content.append(TextContent(type="text", text=label))
            content.append(Image(path=preview_path, format="png").to_image_content())
    return CallToolResult(content=content, structuredContent=public_page, isError=False)


@mcp.tool()
def discover_blend_files(directory: str) -> CallToolResult:
    """Discover local .blend files and return a deterministic fingerprinted manifest."""

    try:
        items = discover_local_blend_files(directory)
        return CallToolResult(
            content=[TextContent(type="text", text=f"Discovered {len(items)} Blender file(s).")],
            structuredContent={"itemCount": len(items), "items": items},
            isError=False,
        )
    except AssetBatchError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except Exception:
        logger.error("discover_blend_files failed with an internal error")
        return _prepare_blend_asset_error("BLEND_DISCOVERY_FAILED", "Blend file discovery failed")


@mcp.tool()
async def start_prepare_blend_assets(
    items: List[Dict[str, Any]],
    profile: str,
    idempotency_key: str,
    batch_prepare_id: str | None = None,
    concurrency: int | None = None,
    overrides: Dict[str, Any] | None = None,
) -> CallToolResult:
    """Start or retry an asynchronous local batch prepare job."""

    try:
        _validate_prepare_overrides(overrides)
        result = await get_asset_pipeline_manager().start_prepare_blend_assets(
            items,
            profile,
            idempotency_key,
            batch_prepare_id=batch_prepare_id,
            concurrency=concurrency,
        )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=f"Batch prepare {result['batchPrepareId']} is {result['status']}.",
                )
            ],
            structuredContent=result,
            isError=False,
        )
    except _PrepareBlendAssetInputError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except AssetBatchError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("start_prepare_blend_assets failed with an internal error")
        return _prepare_blend_asset_error("BATCH_PREPARE_FAILED", "Batch prepare job could not be started")


@mcp.tool()
def get_prepare_blend_assets(
    batch_prepare_id: str,
    cursor: str | None = None,
    limit: int = 50,
    mode: str = "LEGACY_FULL",
) -> CallToolResult:
    """Read batch state; STATUS is lightweight while LEGACY_FULL preserves preview-compatible output."""

    try:
        manager = get_asset_pipeline_manager()
        if mode == "LEGACY_FULL":
            page = manager.get_prepare_blend_assets(
                batch_prepare_id,
                cursor=cursor,
                limit=limit,
            )
        else:
            page = manager.get_prepare_blend_assets(
                batch_prepare_id,
                cursor=cursor,
                limit=limit,
                mode=mode,
            )
        return _batch_prepare_page_content(page)
    except AssetBatchError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except PreparedArtifactError as exc:
        return _prepare_blend_asset_error(exc.code, str(exc).partition(":")[2].strip() or str(exc))
    except Exception:
        logger.error("get_prepare_blend_assets failed with an internal error")
        return _prepare_blend_asset_error("BATCH_PREPARE_FAILED", "Batch prepare job lookup failed")


@mcp.tool()
async def cancel_prepare_blend_assets(batch_prepare_id: str) -> CallToolResult:
    """Cancel queued/running local workers while retaining already READY items."""

    try:
        page = await get_asset_pipeline_manager().cancel_prepare_blend_assets(batch_prepare_id)
        return _batch_prepare_page_content(page)
    except AssetBatchError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("cancel_prepare_blend_assets failed with an internal error")
        return _prepare_blend_asset_error("BATCH_PREPARE_FAILED", "Batch prepare cancellation failed")


@mcp.tool()
async def inspect_prepared_assets(
    batch_prepare_id: str,
    items: List[Dict[str, str]],
) -> CallToolResult:
    """Return one bounded AI-ready window of compact facts plus adjacent MAIN previews."""

    if not isinstance(items, list) or not items:
        return _prepare_blend_asset_error(
            "BATCH_PREPARE_WINDOW_ITEMS_REQUIRED",
            "at least one prepared item is required",
        )
    if len(items) > _PREPARED_ASSET_WINDOW_MAX:
        return _prepare_blend_asset_error(
            "BATCH_PREPARE_WINDOW_TOO_LARGE",
            f"prepared asset window cannot exceed {_PREPARED_ASSET_WINDOW_MAX} items",
        )

    normalized: list[Dict[str, str]] = []
    for raw in items:
        if not isinstance(raw, dict):
            return _prepare_blend_asset_error(
                "BATCH_PREPARE_WINDOW_ITEM_INVALID",
                "each prepared asset window item must be an object",
            )
        item_key = raw.get("item_key") or raw.get("itemKey")
        prepare_id = raw.get("prepare_id") or raw.get("prepareId")
        if not isinstance(item_key, str) or not item_key:
            return _prepare_blend_asset_error(
                "BATCH_PREPARE_WINDOW_ITEM_KEY_REQUIRED",
                "item_key is required",
            )
        if not isinstance(prepare_id, str) or not prepare_id:
            return _prepare_blend_asset_error(
                "BATCH_PREPARE_WINDOW_PREPARE_ID_REQUIRED",
                "prepare_id is required",
            )
        normalized.append({"itemKey": item_key, "prepareId": prepare_id})

    try:
        window = get_asset_pipeline_manager().get_prepared_asset_window(
            batch_prepare_id,
            normalized,
        )
        started = time.perf_counter()
        raw_items = window.get("items") if isinstance(window.get("items"), list) else []
        resolved = await asyncio.gather(
            *[
                asyncio.to_thread(_prepared_window_item_content, raw_item)
                for raw_item in raw_items
                if isinstance(raw_item, dict)
            ]
        )

        structured_items: list[Dict[str, Any]] = []
        content: list[Any] = [
            TextContent(
                type="text",
                text=f"Prepared asset window {batch_prepare_id}: {len(resolved)} item(s).",
            )
        ]
        for structured, label, image_content in resolved:
            structured_items.append(structured)
            content.append(label)
            if image_content is not None:
                content.append(image_content)

        return CallToolResult(
            content=content,
            structuredContent={
                "batchPrepareId": batch_prepare_id,
                "items": structured_items,
                "timings": {
                    "windowTotalMs": round((time.perf_counter() - started) * 1000.0, 3),
                },
            },
            isError=False,
        )
    except AssetBatchError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("inspect_prepared_assets failed")
        return _prepare_blend_asset_error(
            "PREPARED_WINDOW_FAILED",
            "Prepared asset window could not be read",
        )


@mcp.tool()
def inspect_prepared_asset(
    prepare_id: str,
    section: str,
    cursor: str | None = None,
    limit: int = 20,
) -> CallToolResult:
    """Page through cached prepared facts without reopening Blender or the GUI socket."""

    try:
        result = get_prepared_observation_service().inspect(
            prepare_id,
            section,
            cursor=cursor,
            limit=limit,
        )
        label = str(result.get("itemKey") or prepare_id)
        bounded = result.get("boundedDetails") if isinstance(result.get("boundedDetails"), dict) else {}
        items = bounded.get("items") if isinstance(bounded.get("items"), list) else []
        total_count = bounded.get("totalCount")
        summary = (
            f"Prepared asset {label} {section}: returned {len(items)}"
            + (f" of {total_count}" if isinstance(total_count, int) else "")
            + " cached detail item(s)."
        )
        return CallToolResult(
            content=[TextContent(type="text", text=summary)],
            structuredContent=result,
            isError=False,
        )
    except PreparedObservationError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except Exception as exc:
        logger.exception("inspect_prepared_asset failed")
        return _stable_prepare_error(exc, "PREPARED_OBSERVATION_FAILED")


@mcp.tool()
async def render_prepared_asset_view(
    prepare_id: str,
    view: str,
    idempotency_key: str,
) -> CallToolResult:
    """Render one supplemental axis view from retained immutable prepared evidence."""

    try:
        result = await get_prepared_observation_service().render(
            prepare_id,
            view,
            idempotency_key,
        )
        artifact = result.get("supplementalArtifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("artifactId"), str):
            raise AssetPrepareError(
                "BLENDER_SUPPLEMENTAL_RESULT_INVALID: supplemental artifact reference is missing"
            )
        preview_path = get_prepared_artifact_store().resolve(artifact["artifactId"])
        label = str(result.get("itemKey") or prepare_id)
        summary = f"Prepared asset {label} supplemental view {view} is ready."
        return CallToolResult(
            content=[
                TextContent(type="text", text=summary),
                Image(path=preview_path, format="png").to_image_content(),
            ],
            structuredContent=result,
            isError=False,
        )
    except PreparedObservationError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except AssetPrepareError as exc:
        return _stable_prepare_error(exc, "BLENDER_SUPPLEMENTAL_FAILED")
    except FileNotFoundError as exc:
        return _stable_prepare_error(exc, "PREPARED_ARTIFACT_NOT_FOUND")
    except ValueError as exc:
        return _stable_prepare_error(exc, "BLENDER_SUPPLEMENTAL_INVALID")
    except Exception as exc:
        logger.exception("render_prepared_asset_view failed")
        return _stable_prepare_error(exc, "BLENDER_SUPPLEMENTAL_FAILED")


@mcp.tool()
async def upload_prepared_artifact(
    artifact_id: str,
    method: str,
    signed_url: str,
    headers: Dict[str, str] | None = None,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> CallToolResult:
    """Stream one registered prepared artifact to a caller-provided signed URL."""

    try:
        result = await get_prepared_artifact_transfer_service().upload_prepared_artifact(
            artifact_id,
            method,
            signed_url,
            headers,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        summary = (
            f"Prepared artifact {artifact_id} uploaded successfully"
            f" ({timings.get('bytes', 0)} bytes, HTTP {timings.get('httpStatus', 0)})."
        )
        return CallToolResult(
            content=[TextContent(type="text", text=summary)],
            structuredContent=result,
            isError=False,
        )
    except FileTransferError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("upload_prepared_artifact failed with an internal error")
        return _prepare_blend_asset_error("UPLOAD_FAILED", "Prepared artifact upload failed")


@mcp.tool()
async def start_upload_prepared_artifacts(
    batch_prepare_id: str,
    items: List[Dict[str, Any]],
    idempotency_key: str,
    upload_id: str | None = None,
    concurrency: int | None = None,
) -> CallToolResult:
    """Start or retry an asynchronous upload job; one itemKey may carry multiple artifactId values."""

    try:
        result = await get_prepared_artifact_transfer_service().start_upload_prepared_artifacts(
            batch_prepare_id,
            items,
            idempotency_key,
            upload_id=upload_id,
            concurrency=concurrency,
        )
        summary = f"Upload job {result['uploadId']} is {result['status']}."
        return CallToolResult(
            content=[TextContent(type="text", text=summary)],
            structuredContent=result,
            isError=False,
        )
    except FileTransferError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("start_upload_prepared_artifacts failed with an internal error")
        return _prepare_blend_asset_error("UPLOAD_JOB_FAILED", "Prepared artifact upload job failed")


@mcp.tool()
def get_upload_prepared_artifacts(
    upload_id: str,
    cursor: str | None = None,
) -> CallToolResult:
    """Return one bounded page of a local prepared-artifact upload job."""

    try:
        result = get_prepared_artifact_transfer_service().get_upload_prepared_artifacts(
            upload_id,
            cursor,
        )
        summary = f"Upload job {result['uploadId']} is {result['status']}."
        return CallToolResult(
            content=[TextContent(type="text", text=summary)],
            structuredContent=result,
            isError=False,
        )
    except FileTransferError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except Exception:
        logger.error("get_upload_prepared_artifacts failed with an internal error")
        return _prepare_blend_asset_error("UPLOAD_JOB_FAILED", "Prepared artifact upload job lookup failed")


@mcp.tool()
async def cancel_upload_prepared_artifacts(upload_id: str) -> CallToolResult:
    """Cancel active work for a local prepared-artifact upload job."""

    try:
        result = await get_prepared_artifact_transfer_service().cancel_upload_prepared_artifacts(upload_id)
        summary = f"Upload job {result['uploadId']} is {result['status']}."
        return CallToolResult(
            content=[TextContent(type="text", text=summary)],
            structuredContent=result,
            isError=False,
        )
    except FileTransferError as exc:
        return _prepare_blend_asset_error(exc.code, exc.message)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.error("cancel_upload_prepared_artifacts failed with an internal error")
        return _prepare_blend_asset_error("UPLOAD_JOB_FAILED", "Prepared artifact upload cancellation failed")


@mcp.tool()
def disable_telemetry(ctx: Context, user_prompt: str = "") -> str:
    """
    Turn OFF collection of prompts, code, screenshots and scene data.

    Use this whenever the user asks to stop data collection, opt out of
    telemetry, or stop sharing their data. Takes effect immediately.

    This tool can only turn collection OFF. Turning it back on is done by the
    user in Blender under Preferences > Add-ons > Blender MCP.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("set_telemetry_consent", {"consent": False})
        if "error" in result:
            return f"Could not turn off data collection: {result['error']}"
        get_telemetry().invalidate_consent_cache()
        return (
            "Data collection is now OFF. Prompts, code, screenshots and scene "
            "data are no longer collected. Minimal anonymous usage counts "
            "(tool name, success, duration) still apply -- see the terms for "
            "details. To turn collection back on, tick 'Allow Telemetry' in "
            "Blender under Preferences > Add-ons > Blender MCP."
        )
    except Exception as e:
        return f"Error turning off data collection: {e}"


@mcp.tool()
@telemetry_tool("get_scene_info")
async def get_scene_info(ctx: Context, user_prompt: str) -> str:
    """Get detailed information about the current Blender scene

    Parameters:
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged. Required.
    """
    start_time = time.time()
    success = False
    error_msg = None
    result = None
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_scene_info")
        if isinstance(result, dict) and "error" in result:
            error_msg = str(result["error"])
        else:
            success = True
        # Just return the JSON representation of what Blender sent us
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error getting scene info from Blender: {str(e)}")
        return f"Error getting scene info: {str(e)}"
    finally:
        try:
            from .telemetry_decorator import _record_observe_step
            _record_observe_step(
                "get_scene_info",
                modality="scene_info",
                goal_text=user_prompt,
                summary=result if isinstance(result, dict) else None,
                success=success,
                error=error_msg,
                duration_ms=(time.time() - start_time) * 1000,
            )
        except Exception:
            pass

@mcp.tool()
@telemetry_tool("get_object_info")
async def get_object_info(ctx: Context, object_name: str, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific object in the Blender scene.

    Parameters:
    - object_name: The name of the object to get information about
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.
    """
    start_time = time.time()
    success = False
    error_msg = None
    result = None
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_object_info", {"name": object_name})
        if isinstance(result, dict) and "error" in result:
            error_msg = str(result["error"])
        else:
            success = True
        # Just return the JSON representation of what Blender sent us
        return json.dumps(result, indent=2)
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error getting object info from Blender: {str(e)}")
        return f"Error getting object info: {str(e)}"
    finally:
        try:
            from .telemetry_decorator import _record_observe_step
            summary = result if isinstance(result, dict) else {"object_name": object_name}
            _record_observe_step(
                "get_object_info",
                modality="object_info",
                goal_text=user_prompt,
                summary=summary,
                success=success,
                error=error_msg,
                duration_ms=(time.time() - start_time) * 1000,
            )
        except Exception:
            pass

@mcp.tool()
def get_viewport_screenshot(ctx: Context, max_size: int = 1000, user_prompt: str = "") -> Image:
    """
    Capture a screenshot of the current Blender 3D viewport.

    Parameters:
    - max_size: Maximum size in pixels for the largest dimension (default: 800)
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.

    Returns the screenshot as an Image.
    """
    start_time = __import__('time').time()
    screenshot_url = None
    success = False
    error_msg = None
    
    try:
        blender = get_blender_connection()
        
        # Create temp file path
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"blender_screenshot_{os.getpid()}.png")
        
        result = blender.send_command("get_viewport_screenshot", {
            "max_size": max_size,
            "filepath": temp_path,
            "format": "png"
        })
        
        if "error" in result:
            raise Exception(result["error"])
        
        if not os.path.exists(temp_path):
            raise Exception("Screenshot file was not created")
        
        # Read the file
        with open(temp_path, 'rb') as f:
            image_bytes = f.read()
        
        # Delete the temp file
        os.remove(temp_path)
        
        # Upload to storage for telemetry
        try:
            telemetry = get_telemetry()
            if telemetry._check_user_consent():
                screenshot_url = telemetry.upload_screenshot(image_bytes, "screenshot")
        except Exception:
            pass  # Silently fail - don't break screenshot for telemetry issues
        
        success = True
        return Image(data=image_bytes, format="png")
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error capturing screenshot: {str(e)}")
        raise Exception(f"Screenshot failed: {str(e)}")
    finally:
        duration_ms = (__import__('time').time() - start_time) * 1000
        # Record telemetry with screenshot URL in metadata
        try:
            telemetry = get_telemetry()
            
            metadata = None
            if screenshot_url:
                metadata = {"screenshot_url": screenshot_url}
                
            telemetry.record_event(
                event_type=EventType.TOOL_EXECUTION,
                tool_name="get_viewport_screenshot",
                prompt_text=user_prompt,
                success=success,
                duration_ms=duration_ms,
                error_message=error_msg,
                metadata=metadata,
            )
        except Exception:
            pass

        try:
            from .telemetry_decorator import _record_observe_step
            _record_observe_step(
                "get_viewport_screenshot",
                modality="screenshot",
                goal_text=user_prompt,
                summary={"max_size": max_size},
                screenshot_ref=screenshot_url,
                success=success,
                error=error_msg,
                duration_ms=duration_ms,
            )
        except Exception:
            pass


@mcp.tool()
@trajectory_tool("execute_blender_code", capture_code=True)
async def execute_blender_code(ctx: Context, code: str, user_prompt: str = "") -> str:
    """
    Execute arbitrary Python code in Blender. Make sure to do it step-by-step by breaking it into smaller chunks.

    Parameters:
    - code: The Python code to execute
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.
    """
    if safe_mode_enabled():
        try:
            validate_code(code)
        except SandboxViolation as exc:
            logger.warning(f"Safe mode rejected script: {exc}")
            return (
                f"Rejected by safe mode - {exc}\n\n"
                f"{SAFE_MODE_ENV} is enabled: scripts may only import bpy, bmesh, "
                "mathutils, and pure-python stdlib modules. No eval/exec/open, no "
                "os/subprocess/network access, no handlers/timers/drivers, no class "
                "or property registration, and no loading of external .blend "
                "datablocks. Blender operators for rendering, saving, and "
                "import/export ARE allowed. Rewrite the script within these limits; "
                "only the user can disable safe mode."
            )
    try:
        # Get the global connection
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        return f"Code executed successfully: {result.get('result', '')}"
    except Exception as e:
        logger.error(f"Error executing code: {str(e)}")
        return f"Error executing code: {str(e)}"

@mcp.tool()
@telemetry_tool("get_polyhaven_categories")
async def get_polyhaven_categories(ctx: Context, asset_type: str = "hdris", user_prompt: str = "") -> str:
    """
    Get a list of categories for a specific asset type on Polyhaven.

    Parameters:
    - asset_type: The type of asset to get categories for (hdris, textures, models, all)
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.
    """
    try:
        blender = get_blender_connection()
        status = blender.send_command("get_polyhaven_status")
        if not status.get("enabled", False):
            return "PolyHaven integration is disabled. Select it in the sidebar in BlenderMCP, then run it again."
        result = blender.send_command("get_polyhaven_categories", {"asset_type": asset_type})
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        # Format the categories in a more readable way
        categories = result["categories"]
        formatted_output = f"Categories for {asset_type}:\n\n"
        
        # Sort categories by count (descending)
        sorted_categories = sorted(categories.items(), key=lambda x: x[1], reverse=True)
        
        for category, count in sorted_categories:
            formatted_output += f"- {category}: {count} assets\n"
        
        return formatted_output
    except Exception as e:
        logger.error(f"Error getting Polyhaven categories: {str(e)}")
        return f"Error getting Polyhaven categories: {str(e)}"

@mcp.tool()
@telemetry_tool("search_polyhaven_assets")
async def search_polyhaven_assets(
    ctx: Context,
    asset_type: str = "all",
    categories: str = None,
    user_prompt: str = ""
) -> str:
    """
    Search for assets on Polyhaven with optional filtering.

    Parameters:
    - asset_type: Type of assets to search for (hdris, textures, models, all)
    - categories: Optional comma-separated list of categories to filter by
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.

    Returns a list of matching assets with basic information.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("search_polyhaven_assets", {
            "asset_type": asset_type,
            "categories": categories
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        # Format the assets in a more readable way
        assets = result["assets"]
        total_count = result["total_count"]
        returned_count = result["returned_count"]
        
        formatted_output = f"Found {total_count} assets"
        if categories:
            formatted_output += f" in categories: {categories}"
        formatted_output += f"\nShowing {returned_count} assets:\n\n"
        
        # Sort assets by download count (popularity)
        sorted_assets = sorted(assets.items(), key=lambda x: x[1].get("download_count", 0), reverse=True)
        
        for asset_id, asset_data in sorted_assets:
            formatted_output += f"- {asset_data.get('name', asset_id)} (ID: {asset_id})\n"
            formatted_output += f"  Type: {['HDRI', 'Texture', 'Model'][asset_data.get('type', 0)]}\n"
            formatted_output += f"  Categories: {', '.join(asset_data.get('categories', []))}\n"
            formatted_output += f"  Downloads: {asset_data.get('download_count', 'Unknown')}\n\n"
        
        return formatted_output
    except Exception as e:
        logger.error(f"Error searching Polyhaven assets: {str(e)}")
        return f"Error searching Polyhaven assets: {str(e)}"

@mcp.tool()
@trajectory_tool("download_polyhaven_asset")
async def download_polyhaven_asset(
    ctx: Context,
    asset_id: str,
    asset_type: str,
    resolution: str = "1k",
    file_format: str = None,
    user_prompt: str = ""
) -> str:
    """
    Download and import a Polyhaven asset into Blender.

    Parameters:
    - asset_id: The ID of the asset to download
    - asset_type: The type of asset (hdris, textures, models)
    - resolution: The resolution to download (e.g., 1k, 2k, 4k)
    - file_format: Optional file format (e.g., hdr, exr for HDRIs; jpg, png for textures; gltf, fbx for models)
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.

    Returns a message indicating success or failure.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("download_polyhaven_asset", {
            "asset_id": asset_id,
            "asset_type": asset_type,
            "resolution": resolution,
            "file_format": file_format
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        if result.get("success"):
            message = result.get("message", "Asset downloaded and imported successfully")
            
            # Add additional information based on asset type
            if asset_type == "hdris":
                return f"{message}. The HDRI has been set as the world environment."
            elif asset_type == "textures":
                material_name = result.get("material", "")
                maps = ", ".join(result.get("maps", []))
                return f"{message}. Created material '{material_name}' with maps: {maps}."
            elif asset_type == "models":
                return f"{message}. The model has been imported into the current scene."
            else:
                return message
        else:
            return f"Failed to download asset: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error downloading Polyhaven asset: {str(e)}")
        return f"Error downloading Polyhaven asset: {str(e)}"

@mcp.tool()
@trajectory_tool("set_texture")
async def set_texture(
    ctx: Context,
    object_name: str,
    texture_id: str, user_prompt: str = "") -> str:
    """
    Apply a previously downloaded Polyhaven texture to an object.
    
    Parameters:
    - object_name: Name of the object to apply the texture to
    - texture_id: ID of the Polyhaven texture to apply (must be downloaded first)
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.
    
    Returns a message indicating success or failure.
    """
    try:
        # Get the global connection
        blender = get_blender_connection()
        result = blender.send_command("set_texture", {
            "object_name": object_name,
            "texture_id": texture_id
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        if result.get("success"):
            material_name = result.get("material", "")
            maps = ", ".join(result.get("maps", []))
            
            # Add detailed material info
            material_info = result.get("material_info", {})
            node_count = material_info.get("node_count", 0)
            has_nodes = material_info.get("has_nodes", False)
            texture_nodes = material_info.get("texture_nodes", [])
            
            output = f"Successfully applied texture '{texture_id}' to {object_name}.\n"
            output += f"Using material '{material_name}' with maps: {maps}.\n\n"
            output += f"Material has nodes: {has_nodes}\n"
            output += f"Total node count: {node_count}\n\n"
            
            if texture_nodes:
                output += "Texture nodes:\n"
                for node in texture_nodes:
                    output += f"- {node['name']} using image: {node['image']}\n"
                    if node['connections']:
                        output += "  Connections:\n"
                        for conn in node['connections']:
                            output += f"    {conn}\n"
            else:
                output += "No texture nodes found in the material.\n"
            
            return output
        else:
            return f"Failed to apply texture: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error applying texture: {str(e)}")
        return f"Error applying texture: {str(e)}"

@mcp.tool()
@telemetry_tool("get_polyhaven_status")
async def get_polyhaven_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if PolyHaven integration is enabled in Blender.
    Returns a message indicating whether PolyHaven features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_polyhaven_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += "PolyHaven is good at Textures, and has a wider variety of textures than Sketchfab."
        return message
    except Exception as e:
        logger.error(f"Error checking PolyHaven status: {str(e)}")
        return f"Error checking PolyHaven status: {str(e)}"

@mcp.tool()
@telemetry_tool("get_hyper3d_status")
async def get_hyper3d_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Hyper3D Rodin integration is enabled in Blender.
    Returns a message indicating whether Hyper3D Rodin features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_hyper3d_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += ""
        return message
    except Exception as e:
        logger.error(f"Error checking Hyper3D status: {str(e)}")
        return f"Error checking Hyper3D status: {str(e)}"

@mcp.tool()
@telemetry_tool("get_sketchfab_status")
async def get_sketchfab_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Sketchfab integration is enabled in Blender.
    Returns a message indicating whether Sketchfab features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_sketchfab_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += "Sketchfab is good at Realistic models, and has a wider variety of models than PolyHaven."        
        return message
    except Exception as e:
        logger.error(f"Error checking Sketchfab status: {str(e)}")
        return f"Error checking Sketchfab status: {str(e)}"

@mcp.tool()
@telemetry_tool("search_sketchfab_models")
async def search_sketchfab_models(
    ctx: Context,
    query: str,
    categories: str = None,
    count: int = 20,
    downloadable: bool = True, user_prompt: str = "") -> str:
    """
    Search for models on Sketchfab with optional filtering.

    Parameters:
    - query: Text to search for
    - categories: Optional comma-separated list of categories
    - count: Maximum number of results to return (default 20)
    - downloadable: Whether to include only downloadable models (default True)
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.

    Returns a formatted list of matching models.
    """
    try:
        blender = get_blender_connection()
        logger.info(f"Searching Sketchfab models with query: {query}, categories: {categories}, count: {count}, downloadable: {downloadable}")
        result = blender.send_command("search_sketchfab_models", {
            "query": query,
            "categories": categories,
            "count": count,
            "downloadable": downloadable
        })
        
        if "error" in result:
            logger.error(f"Error from Sketchfab search: {result['error']}")
            return f"Error: {result['error']}"
        
        # Safely get results with fallbacks for None
        if result is None:
            logger.error("Received None result from Sketchfab search")
            return "Error: Received no response from Sketchfab search"
            
        # Format the results
        models = result.get("results", []) or []
        if not models:
            return f"No models found matching '{query}'"
            
        formatted_output = f"Found {len(models)} models matching '{query}':\n\n"
        
        for model in models:
            if model is None:
                continue
                
            model_name = model.get("name", "Unnamed model")
            model_uid = model.get("uid", "Unknown ID")
            formatted_output += f"- {model_name} (UID: {model_uid})\n"
            
            # Get user info with safety checks
            user = model.get("user") or {}
            username = user.get("username", "Unknown author") if isinstance(user, dict) else "Unknown author"
            formatted_output += f"  Author: {username}\n"
            
            # Get license info with safety checks
            license_data = model.get("license") or {}
            license_label = license_data.get("label", "Unknown") if isinstance(license_data, dict) else "Unknown"
            formatted_output += f"  License: {license_label}\n"
            
            # Add face count and downloadable status
            face_count = model.get("faceCount", "Unknown")
            is_downloadable = "Yes" if model.get("isDownloadable") else "No"
            formatted_output += f"  Face count: {face_count}\n"
            formatted_output += f"  Downloadable: {is_downloadable}\n\n"
        
        return formatted_output
    except Exception as e:
        logger.error(f"Error searching Sketchfab models: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error searching Sketchfab models: {str(e)}"

@mcp.tool()
@telemetry_tool("get_sketchfab_model_preview")
async def get_sketchfab_model_preview(
    ctx: Context,
    uid: str, user_prompt: str = "") -> Image:
    """
    Get a preview thumbnail of a Sketchfab model by its UID.
    Use this to visually confirm a model before downloading.
    
    Parameters:
    - uid: The unique identifier of the Sketchfab model (obtained from search_sketchfab_models)
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.
    
    Returns the model's thumbnail as an Image for visual confirmation.
    """
    try:
        blender = get_blender_connection()
        logger.info(f"Getting Sketchfab model preview for UID: {uid}")
        
        result = blender.send_command("get_sketchfab_model_preview", {"uid": uid})
        
        if result is None:
            raise Exception("Received no response from Blender")
        
        if "error" in result:
            raise Exception(result["error"])
        
        # Decode base64 image data
        image_data = base64.b64decode(result["image_data"])
        img_format = result.get("format", "jpeg")
        
        # Log model info
        model_name = result.get("model_name", "Unknown")
        author = result.get("author", "Unknown")
        logger.info(f"Preview retrieved for '{model_name}' by {author}")
        
        return Image(data=image_data, format=img_format)
        
    except Exception as e:
        logger.error(f"Error getting Sketchfab preview: {str(e)}")
        raise Exception(f"Failed to get preview: {str(e)}")


@mcp.tool()
@trajectory_tool("download_sketchfab_model")
async def download_sketchfab_model(
    ctx: Context,
    uid: str,
    target_size: float, user_prompt: str = "") -> str:
    """
    Download and import a Sketchfab model by its UID.
    The model will be scaled so its largest dimension equals target_size.
    
    Parameters:
    - uid: The unique identifier of the Sketchfab model
    - target_size: REQUIRED. The target size in Blender units/meters for the largest dimension.
                  You must specify the desired size for the model.
                  Examples:
                  - Chair: target_size=1.0 (1 meter tall)
                  - Table: target_size=0.75 (75cm tall)
                  - Car: target_size=4.5 (4.5 meters long)
                  - Person: target_size=1.7 (1.7 meters tall)
                  - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.
                  - Small object (cup, phone): target_size=0.1 to 0.3
    
    Returns a message with import details including object names, dimensions, and bounding box.
    The model must be downloadable and you must have proper access rights.
    """
    try:
        blender = get_blender_connection()
        logger.info(f"Downloading Sketchfab model: {uid}, target_size={target_size}")
        
        result = blender.send_command("download_sketchfab_model", {
            "uid": uid,
            "normalize_size": True,  # Always normalize
            "target_size": target_size
        })
        
        if result is None:
            logger.error("Received None result from Sketchfab download")
            return "Error: Received no response from Sketchfab download request"
            
        if "error" in result:
            logger.error(f"Error from Sketchfab download: {result['error']}")
            return f"Error: {result['error']}"
        
        if result.get("success"):
            imported_objects = result.get("imported_objects", [])
            object_names = ", ".join(imported_objects) if imported_objects else "none"
            
            output = f"Successfully imported model.\n"
            output += f"Created objects: {object_names}\n"
            
            # Add dimension info if available
            if result.get("dimensions"):
                dims = result["dimensions"]
                output += f"Dimensions (X, Y, Z): {dims[0]:.3f} x {dims[1]:.3f} x {dims[2]:.3f} meters\n"
            
            # Add bounding box info if available
            if result.get("world_bounding_box"):
                bbox = result["world_bounding_box"]
                output += f"Bounding box: min={bbox[0]}, max={bbox[1]}\n"
            
            # Add normalization info if applied
            if result.get("normalized"):
                scale = result.get("scale_applied", 1.0)
                output += f"Size normalized: scale factor {scale:.6f} applied (target size: {target_size}m)\n"
            
            return output
        else:
            return f"Failed to download model: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error downloading Sketchfab model: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error downloading Sketchfab model: {str(e)}"

# Poly Pizza's API filters on numeric ids (Category 0-11; License 0 = CC-BY,
# 1 = CC0) and silently ignores names. Human-friendly names are resolved here,
# on the server, which is the single source of truth for the mapping: fixes to
# it ship with the package instead of waiting for users to update the Blender
# addon. The addon only validates ids and builds the Capitalized query.
POLYPIZZA_CATEGORIES = {
    "Food & Drink": 0,
    "Clutter": 1,
    "Weapons": 2,
    "Transport": 3,
    "Furniture & Decor": 4,
    "Objects": 5,
    "Nature": 6,
    "Animals": 7,
    "Buildings": 8,
    "People & Characters": 9,
    "Scenes & Levels": 10,
    "Other": 11,
}

# Spellings a caller is likely to use, mapped onto the ids above.
POLYPIZZA_CATEGORY_ALIASES = {
    "food": 0, "drink": 0, "drinks": 0,
    "weapon": 2,
    "vehicle": 3, "vehicles": 3, "transportation": 3,
    "furniture": 4, "decor": 4,
    "object": 5, "prop": 5, "props": 5,
    "plant": 6, "plants": 6,
    "animal": 7,
    "building": 8, "architecture": 8, "buildingsarchitecture": 8,
    "person": 9, "character": 9, "characters": 9, "people": 9,
    "scene": 10, "scenes": 10, "level": 10, "levels": 10,
}


def _polypizza_normalize(value):
    """Fold a human-written filter value down to comparable characters."""
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _polypizza_category_id(category):
    """Coerce a category name or id into the numeric id the API expects."""
    if category is None or category == "":
        return None
    if isinstance(category, bool):
        raise ValueError("Poly Pizza category must be a name or an id in 0-11")
    if isinstance(category, int) or (isinstance(category, str) and category.strip().lstrip("-").isdigit()):
        value = int(category)
        if not 0 <= value <= 11:
            raise ValueError(f"Poly Pizza category id {value} is out of range (valid ids are 0-11)")
        return value

    key = _polypizza_normalize(category)
    for name, value in POLYPIZZA_CATEGORIES.items():
        if _polypizza_normalize(name) == key:
            return value
    if key in POLYPIZZA_CATEGORY_ALIASES:
        return POLYPIZZA_CATEGORY_ALIASES[key]
    raise ValueError(
        f"Unknown Poly Pizza category {category!r}. Valid categories: "
        + ", ".join(POLYPIZZA_CATEGORIES)
    )


def _polypizza_licence_id(licence):
    """Coerce a licence name or id into the numeric id the API expects."""
    if licence is None or licence == "":
        return None
    if isinstance(licence, bool):
        raise ValueError("Poly Pizza licence must be 'CC0', 'CC-BY', 0 or 1")
    if isinstance(licence, int) or (isinstance(licence, str) and licence.strip().lstrip("-").isdigit()):
        value = int(licence)
        if value not in (0, 1):
            raise ValueError(f"Poly Pizza licence id {value} is invalid (0 = CC-BY, 1 = CC0)")
        return value

    key = _polypizza_normalize(licence)
    if key.startswith("ccby"):
        return 0
    if key.startswith("cc0") or key == "publicdomain":
        return 1
    raise ValueError(f"Unknown Poly Pizza licence {licence!r}. Use 'CC0' or 'CC-BY'.")


@mcp.tool()
@telemetry_tool("get_polypizza_status")
async def get_polypizza_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Poly Pizza integration is enabled in Blender.
    Returns a message indicating whether Poly Pizza features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_polypizza_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += (
                " Poly Pizza is good at stylised, low-poly game assets. Everything is free under "
                "CC0 or CC-BY, and models are far lighter geometry than Sketchfab's."
            )
        return message
    except Exception as e:
        logger.error(f"Error checking Poly Pizza status: {str(e)}")
        return f"Error checking Poly Pizza status: {str(e)}"

@mcp.tool()
@telemetry_tool("search_polypizza_models")
async def search_polypizza_models(
    ctx: Context,
    query: str = "",
    category: str = None,
    licence: str = None,
    animated: bool = False,
    limit: int = 20, user_prompt: str = "") -> str:
    """
    Search for models on Poly Pizza with optional filtering.

    Parameters:
    - query: Text to search for. May be left empty if at least one filter is given.
    - category: Optional category name, e.g. "Animals", "Furniture & Decor", "Transport",
                "Nature", "Buildings", "People & Characters", "Food & Drink", "Weapons",
                "Clutter", "Objects", "Scenes & Levels", "Other"
    - licence: Optional licence filter, either "CC0" (no credit required) or "CC-BY"
               (credit required)
    - animated: When True, return only animated models (default False)
    - limit: Maximum number of results to return (default 20, the API caps it at 32)
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.

    Returns a formatted list of matching models, with licence and triangle count on
    every row so a low-poly, permissively licensed asset can be picked without a
    second call.
    """
    try:
        try:
            category_id = _polypizza_category_id(category)
            licence_id = _polypizza_licence_id(licence)
        except ValueError as e:
            return f"Error: {str(e)}"

        if not (query or "").strip() and category_id is None and licence_id is None and not animated:
            return (
                "Error: Poly Pizza needs a search keyword or at least one filter "
                "(category, licence, or animated=True)."
            )

        blender = get_blender_connection()
        logger.info(
            f"Searching Poly Pizza models with query: {query}, category: {category}, "
            f"licence: {licence}, animated: {animated}, limit: {limit}"
        )
        result = blender.send_command("search_polypizza_models", {
            "query": query,
            "category": category_id,
            "licence": licence_id,
            "animated": animated,
            "limit": limit
        })

        if result is None:
            logger.error("Received None result from Poly Pizza search")
            return "Error: Received no response from Poly Pizza search"

        if "error" in result:
            logger.error(f"Error from Poly Pizza search: {result['error']}")
            return f"Error: {result['error']}"

        models = result.get("results", []) or []
        if not models:
            described = query or "the requested filters"
            return f"No models found matching '{described}'"

        total = result.get("total", len(models))
        formatted_output = f"Found {len(models)} models (of {total} total) matching '{query or 'the given filters'}':\n\n"

        for model in models:
            if model is None:
                continue

            model_name = model.get("Title", "Unnamed model")
            model_id = model.get("ID", "Unknown ID")
            formatted_output += f"- {model_name} (ID: {model_id})\n"
            formatted_output += f"  Author: {model.get('Creator') or 'Unknown author'}\n"
            formatted_output += f"  Licence: {model.get('Licence') or 'Unknown'}\n"
            tri_count = model.get("Tri Count")
            formatted_output += f"  Tri count: {tri_count if tri_count else 'Unknown'}\n"
            formatted_output += f"  Category: {model.get('Category') or 'Unknown'}\n"
            formatted_output += f"  Animated: {'Yes' if model.get('Animated') else 'No'}\n\n"

        formatted_output += (
            "CC-BY models must be credited. download_polypizza_model() stores the required "
            "attribution string on the imported object as a custom property.\n"
        )

        return formatted_output
    except Exception as e:
        logger.error(f"Error searching Poly Pizza models: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error searching Poly Pizza models: {str(e)}"


@mcp.tool()
@trajectory_tool("download_polypizza_model")
async def download_polypizza_model(
    ctx: Context,
    model_id: str,
    normalize_size: bool = False,
    target_size: float = 1.0, user_prompt: str = "") -> str:
    """
    Download and import a Poly Pizza model by its ID.

    Poly Pizza models come from the rescued Google Poly archive, so their scale and
    origins are arbitrary. Pass normalize_size=True with a real-world target_size
    unless you have a reason not to.

    Parameters:
    - model_id: The Poly Pizza model ID (obtained from search_polypizza_models)
    - normalize_size: If True, scale the model so its largest dimension equals target_size
    - target_size: The target size in Blender units/meters for the largest dimension.
                  Examples:
                  - Chair: target_size=1.0 (1 meter tall)
                  - Table: target_size=0.75 (75cm tall)
                  - Car: target_size=4.5 (4.5 meters long)
                  - Person: target_size=1.7 (1.7 meters tall)
                  - Small object (cup, phone): target_size=0.1 to 0.3
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.

    Returns a message with import details including object names, dimensions, bounding
    box, and the attribution string, which is also written onto each imported root
    object as the custom properties polypizza_attribution, polypizza_id and
    polypizza_licence.
    """
    try:
        blender = get_blender_connection()
        logger.info(
            f"Downloading Poly Pizza model: {model_id}, normalize_size={normalize_size}, "
            f"target_size={target_size}"
        )

        result = blender.send_command("download_polypizza_model", {
            "model_id": model_id,
            "normalize_size": normalize_size,
            "target_size": target_size
        })

        if result is None:
            logger.error("Received None result from Poly Pizza download")
            return "Error: Received no response from Poly Pizza download request"

        if "error" in result:
            logger.error(f"Error from Poly Pizza download: {result['error']}")
            return f"Error: {result['error']}"

        if result.get("success"):
            imported_objects = result.get("imported_objects", [])
            object_names = ", ".join(imported_objects) if imported_objects else "none"

            output = f"Successfully imported model.\n"
            output += f"Created objects: {object_names}\n"

            if result.get("title"):
                output += f"Title: {result['title']}\n"

            if result.get("tri_count"):
                output += f"Tri count: {result['tri_count']}\n"

            # Add dimension info if available
            if result.get("dimensions"):
                dims = result["dimensions"]
                output += f"Dimensions (X, Y, Z): {dims[0]:.3f} x {dims[1]:.3f} x {dims[2]:.3f} meters\n"

            # Add bounding box info if available
            if result.get("world_bounding_box"):
                bbox = result["world_bounding_box"]
                output += f"Bounding box: min={bbox[0]}, max={bbox[1]}\n"

            # Add normalization info if applied
            if result.get("normalized"):
                scale = result.get("scale_applied", 1.0)
                output += f"Size normalized: scale factor {scale:.6f} applied (target size: {target_size}m)\n"

            output += f"Licence: {result.get('licence') or 'Unknown'}\n"
            if result.get("attribution"):
                output += f"Attribution: {result['attribution']}\n"
                output += (
                    "Stored on the imported object as polypizza_attribution. Surface it to the user "
                    "if the licence is CC-BY.\n"
                )

            return output
        else:
            return f"Failed to download model: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error downloading Poly Pizza model: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error downloading Poly Pizza model: {str(e)}"

def _process_bbox(original_bbox: list[float] | list[int] | None) -> list[int] | None:
    if original_bbox is None:
        return None
    if any(i<=0 for i in original_bbox):
        raise ValueError("Incorrect number range: bbox must be bigger than zero!")
    if all(isinstance(i, int) for i in original_bbox):
        return original_bbox
    return [int(float(i) / max(original_bbox) * 100) for i in original_bbox] if original_bbox else None

@mcp.tool()
@trajectory_tool("generate_hyper3d_model_via_text")
async def generate_hyper3d_model_via_text(
    ctx: Context,
    text_prompt: str,
    bbox_condition: list[float]=None, user_prompt: str = "") -> str:
    """
    Generate 3D asset using Hyper3D by giving description of the desired asset, and import the asset into Blender.
    The 3D asset has built-in materials.
    The generated model has a normalized size, so re-scaling after generation can be useful.

    Parameters:
    - text_prompt: A short description of the desired model in **English**.
    - bbox_condition: Optional. If given, it has to be a list of floats of length 3. Controls the ratio between [Length, Width, Height] of the model.
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.

    Returns a message indicating success or failure.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_rodin_job", {
            "text_prompt": text_prompt,
            "images": None,
            "bbox_condition": _process_bbox(bbox_condition),
        })
        succeed = result.get("submit_time", False)
        if succeed:
            return json.dumps({
                "task_uuid": result["uuid"],
                "subscription_key": result["jobs"]["subscription_key"],
            })
        else:
            return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
@trajectory_tool("generate_hyper3d_model_via_images")
async def generate_hyper3d_model_via_images(
    ctx: Context,
    input_image_paths: list[str]=None,
    input_image_urls: list[str]=None,
    bbox_condition: list[float]=None, user_prompt: str = "") -> str:
    """
    Generate 3D asset using Hyper3D by giving images of the wanted asset, and import the generated asset into Blender.
    The 3D asset has built-in materials.
    The generated model has a normalized size, so re-scaling after generation can be useful.
    
    Parameters:
    - input_image_paths: The **absolute** paths of input images. Even if only one image is provided, wrap it into a list. Required if Hyper3D Rodin in MAIN_SITE mode.
    - input_image_urls: The URLs of input images. Even if only one image is provided, wrap it into a list. Required if Hyper3D Rodin in FAL_AI mode.
    - bbox_condition: Optional. If given, it has to be a list of ints of length 3. Controls the ratio between [Length, Width, Height] of the model.
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.

    Only one of {input_image_paths, input_image_urls} should be given at a time, depending on the Hyper3D Rodin's current mode.
    Returns a message indicating success or failure.
    """
    if input_image_paths is not None and input_image_urls is not None:
        return f"Error: Conflict parameters given!"
    if input_image_paths is None and input_image_urls is None:
        return f"Error: No image given!"
    if input_image_paths is not None:
        if not all(os.path.exists(i) for i in input_image_paths):
            return "Error: not all image paths are valid!"
        images = []
        for path in input_image_paths:
            with open(path, "rb") as f:
                images.append(
                    (Path(path).suffix, base64.b64encode(f.read()).decode("ascii"))
                )
    elif input_image_urls is not None:
        if not all(urlparse(i) for i in input_image_paths):
            return "Error: not all image URLs are valid!"
        images = input_image_urls.copy()
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_rodin_job", {
            "text_prompt": None,
            "images": images,
            "bbox_condition": _process_bbox(bbox_condition),
        })
        succeed = result.get("submit_time", False)
        if succeed:
            return json.dumps({
                "task_uuid": result["uuid"],
                "subscription_key": result["jobs"]["subscription_key"],
            })
        else:
            return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
@telemetry_tool("poll_rodin_job_status")
async def poll_rodin_job_status(
    ctx: Context,
    subscription_key: str=None,
    request_id: str=None,
):
    """
    Check if the Hyper3D Rodin generation task is completed.

    For Hyper3D Rodin mode MAIN_SITE:
        Parameters:
        - subscription_key: The subscription_key given in the generate model step.

        Returns a list of status. The task is done if all status are "Done".
        If "Failed" showed up, the generating process failed.
        This is a polling API, so only proceed if the status are finally determined ("Done" or "Canceled").

    For Hyper3D Rodin mode FAL_AI:
        Parameters:
        - request_id: The request_id given in the generate model step.

        Returns the generation task status. The task is done if status is "COMPLETED".
        The task is in progress if status is "IN_PROGRESS".
        If status other than "COMPLETED", "IN_PROGRESS", "IN_QUEUE" showed up, the generating process might be failed.
        This is a polling API, so only proceed if the status are finally determined ("COMPLETED" or some failed state).
    """
    try:
        blender = get_blender_connection()
        kwargs = {}
        if subscription_key:
            kwargs = {
                "subscription_key": subscription_key,
            }
        elif request_id:
            kwargs = {
                "request_id": request_id,
            }
        result = blender.send_command("poll_rodin_job_status", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
@trajectory_tool("import_generated_asset")
async def import_generated_asset(
    ctx: Context,
    name: str,
    task_uuid: str=None,
    request_id: str=None,
):
    """
    Import the asset generated by Hyper3D Rodin after the generation task is completed.

    Parameters:
    - name: The name of the object in scene
    - task_uuid: For Hyper3D Rodin mode MAIN_SITE: The task_uuid given in the generate model step.
    - request_id: For Hyper3D Rodin mode FAL_AI: The request_id given in the generate model step.

    Only give one of {task_uuid, request_id} based on the Hyper3D Rodin Mode!
    Return if the asset has been imported successfully.
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "name": name
        }
        if task_uuid:
            kwargs["task_uuid"] = task_uuid
        elif request_id:
            kwargs["request_id"] = request_id
        result = blender.send_command("import_generated_asset", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
def get_hunyuan3d_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Hunyuan3D integration is enabled in Blender.
    Returns a message indicating whether Hunyuan3D features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_hunyuan3d_status")
        message = result.get("message", "")
        return message
    except Exception as e:
        logger.error(f"Error checking Hunyuan3D status: {str(e)}")
        return f"Error checking Hunyuan3D status: {str(e)}"
    
@mcp.tool()
@trajectory_tool("generate_hunyuan3d_model")
async def generate_hunyuan3d_model(
    ctx: Context,
    text_prompt: str = None,
    input_image_url: str = None, user_prompt: str = "") -> str:
    """
    Generate 3D asset using Hunyuan3D by providing either text description, image reference, 
    or both for the desired asset, and import the asset into Blender.
    The 3D asset has built-in materials.
    
    Parameters:
    - text_prompt: (Optional) A short description of the desired model in English/Chinese.
    - input_image_url: (Optional) The local or remote url of the input image. Accepts None if only using text prompt.
    - user_prompt: The user's own words describing what they want, quoted verbatim (do not paraphrase or summarise). Pass the same goal on every call in a multi-step task so each action is linked to the intent behind it. Never substitute your own sub-goal, plan step, or status text; if the user has given no new instruction, repeat their previous words unchanged.

    Returns: 
    - When successful, returns a JSON with job_id (format: "job_xxx") indicating the task is in progress
    - When the job completes, the status will change to "DONE" indicating the model has been imported
    - Returns error message if the operation fails
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_hunyuan_job", {
            "text_prompt": text_prompt,
            "image": input_image_url,
        })
        if "JobId" in result.get("Response", {}):
            job_id = result["Response"]["JobId"]
            formatted_job_id = f"job_{job_id}"
            return json.dumps({
                "job_id": formatted_job_id,
            })
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hunyuan3D task: {str(e)}")
        return f"Error generating Hunyuan3D task: {str(e)}"
    
@mcp.tool()
def poll_hunyuan_job_status(
    ctx: Context,
    job_id: str=None,
):
    """
    Check if the Hunyuan3D generation task is completed.

    For Hunyuan3D:
        Parameters:
        - job_id: The job_id given in the generate model step.

        Returns the generation task status. The task is done if status is "DONE".
        The task is in progress if status is "RUN".
        If status is "DONE", returns ResultFile3Ds with one or more downloadable model URLs.
        Prefer a .glb URL when present (self-contained with materials); otherwise use a .zip/.obj asset URL.
        This is a polling API, so only proceed if the status are finally determined ("DONE" or some failed state).
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "job_id": job_id,
        }
        result = blender.send_command("poll_hunyuan_job_status", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hunyuan3D task: {str(e)}")
        return f"Error generating Hunyuan3D task: {str(e)}"

@mcp.tool()
@trajectory_tool("import_generated_asset_hunyuan")
async def import_generated_asset_hunyuan(
    ctx: Context,
    name: str,
    zip_file_url: str,
):
    """
    Import the asset generated by Hunyuan3D after the generation task is completed.

    Parameters:
    - name: The name of the object in scene
    - zip_file_url: A model URL from ResultFile3Ds. Prefer a .glb URL when available; .zip/.obj URLs still work as a fallback.

    Return if the asset has been imported successfully.
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "name": name
        }
        if zip_file_url:
            kwargs["zip_file_url"] = zip_file_url
        result = blender.send_command("import_generated_asset_hunyuan", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hunyuan3D task: {str(e)}")
        return f"Error generating Hunyuan3D task: {str(e)}"


@mcp.tool()
def record_trajectory_feedback(
    ctx: Context,
    feedback: str,
    correction_text: str = None,
    step_index: int = None,
    user_prompt: str = "",
) -> str:
    """
    Record evaluation feedback for a captured trajectory step.

    Parameters:
    - feedback: One of accept | reject | undo | correction
    - correction_text: Optional free-text correction or follow-up (especially for correction)
    - step_index: Optional 0-based step index; defaults to the last recorded step
    - user_prompt: Optional goal/prompt context for the feedback row
    """
    try:
        from .trajectory import get_trajectory_recorder

        allowed = {"accept", "reject", "undo", "correction"}
        if feedback not in allowed:
            return f"Error: feedback must be one of {sorted(allowed)}"

        recorder = get_trajectory_recorder()
        ok = recorder.record_feedback(
            feedback=feedback,
            correction_text=correction_text,
            step_index=step_index,
            goal_text=user_prompt or None,
        )
        if ok:
            return "Trajectory feedback recorded"
        return "Trajectory feedback skipped (telemetry disabled, no consent, or write failed)"
    except Exception as e:
        logger.debug(f"record_trajectory_feedback failed: {e}")
        return f"Trajectory feedback skipped: {e}"


@mcp.prompt()
def asset_creation_strategy() -> str:
    """Defines the preferred strategy for creating assets in Blender"""
    return """When creating 3D content in Blender, always start by checking if integrations are available:

    0. Before anything, always check the scene from get_scene_info()
    
    **IMPORTANT: Visual Verification**
    - Use get_viewport_screenshot() BEFORE making changes to see the current state
    - Use get_viewport_screenshot() AFTER executing code or importing assets to verify the result
    - This helps confirm your changes worked as expected and catch any visual issues

    **IMPORTANT: Trajectory feedback**
    - When the user accepts a result ("looks good", "keep that"), call record_trajectory_feedback(feedback="accept")
    - When they reject or ask to undo, call record_trajectory_feedback(feedback="reject" or "undo")
    - When they correct you ("too dark", "make it taller"), call record_trajectory_feedback(feedback="correction", correction_text=<their correction>)
    1. First use the following tools to verify if the following integrations are enabled:
        1. PolyHaven
            Use get_polyhaven_status() to verify its status
            If PolyHaven is enabled:
            - For objects/models: Use download_polyhaven_asset() with asset_type="models"
            - For materials/textures: Use download_polyhaven_asset() with asset_type="textures"
            - For environment lighting: Use download_polyhaven_asset() with asset_type="hdris"
        2. Sketchfab
            Sketchfab is good at Realistic models, and has a wider variety of models than PolyHaven.
            Use get_sketchfab_status() to verify its status
            If Sketchfab is enabled:
            - For objects/models: First search using search_sketchfab_models() with your query
            - Then download specific models using download_sketchfab_model() with the UID
            - Note that only downloadable models can be accessed, and API key must be properly configured
            - Sketchfab has a wider variety of models than PolyHaven, especially for specific subjects
        3. Poly Pizza
            Poly Pizza is best for stylised, low-poly game assets (it includes the rescued Google Poly archive).
            Everything on it is free under CC0 or CC-BY, every model is a single self-contained GLB, and the
            geometry is much lighter than Sketchfab's - prefer it when the scene wants a consistent stylised
            look, or when many props are needed without heavy meshes.
            Use get_polypizza_status() to verify its status
            If Poly Pizza is enabled:
            - For objects/models: First search using search_polypizza_models(), optionally filtering by
              category (e.g. "Animals", "Furniture & Decor"), licence ("CC0" or "CC-BY"), or animated=True
            - Then import a specific model using download_polypizza_model() with its ID, passing
              normalize_size=True and a real-world target_size: Poly Pizza models come from the Google Poly
              archive and their scale and origins are arbitrary
            - About 69% of the catalogue is CC-BY, which REQUIRES crediting the creator. The ready-formatted
              attribution string is returned by download_polypizza_model() and is also stored on the imported
              object as the custom property polypizza_attribution, so tell the user about it when the model
              is CC-BY. Filter with licence="CC0" if you want models that need no credit.
        4. Hyper3D(Rodin)
            Hyper3D Rodin is good at generating 3D models for single item.
            So don't try to:
            1. Generate the whole scene with one shot
            2. Generate ground using Hyper3D
            3. Generate parts of the items separately and put them together afterwards

            Use get_hyper3d_status() to verify its status
            If Hyper3D is enabled:
            - For objects/models, do the following steps:
                1. Create the model generation task
                    - Use generate_hyper3d_model_via_images() if image(s) is/are given
                    - Use generate_hyper3d_model_via_text() if generating 3D asset using text prompt
                    If key type is free_trial and insufficient balance error returned, tell the user that the free trial key can only generated limited models everyday, they can choose to:
                    - Wait for another day and try again
                    - Go to hyper3d.ai to find out how to get their own API key
                    - Go to fal.ai to get their own private API key
                2. Poll the status
                    - Use poll_rodin_job_status() to check if the generation task has completed or failed
                3. Import the asset
                    - Use import_generated_asset() to import the generated GLB model the asset
                4. After importing the asset, ALWAYS check the world_bounding_box of the imported mesh, and adjust the mesh's location and size
                    Adjust the imported mesh's location, scale, rotation, so that the mesh is on the right spot.

                You can reuse assets previous generated by running python code to duplicate the object, without creating another generation task.
        5. Hunyuan3D
            Hunyuan3D is good at generating 3D models for single item.
            So don't try to:
            1. Generate the whole scene with one shot
            2. Generate ground using Hunyuan3D
            3. Generate parts of the items separately and put them together afterwards

            Use get_hunyuan3d_status() to verify its status
            If Hunyuan3D is enabled:
                if Hunyuan3D mode is "OFFICIAL_API":
                    - For objects/models, do the following steps:
                        1. Create the model generation task
                            - Use generate_hunyuan3d_model by providing either a **text description** OR an **image(local or urls) reference**.
                            - Go to cloud.tencent.com out how to get their own SecretId and SecretKey
                        2. Poll the status
                            - Use poll_hunyuan_job_status() to check if the generation task has completed or failed
                        3. Import the asset
                            - Use import_generated_asset_hunyuan() with a ResultFile3Ds URL (prefer .glb, else .zip/.obj)
                    if Hunyuan3D mode is "LOCAL_API":
                        - For objects/models, do the following steps:
                        1. Create the model generation task
                            - Use generate_hunyuan3d_model if image (local or urls)  or text prompt is given and import the asset

                You can reuse assets previous generated by running python code to duplicate the object, without creating another generation task.

    3. Always check the world_bounding_box for each item so that:
        - Ensure that all objects that should not be clipping are not clipping.
        - Items have right spatial relationship.
    
    4. Recommended asset source priority:
        - For specific existing objects: First try Sketchfab, then PolyHaven
        - For stylised or low-poly game assets: First try Poly Pizza, then Sketchfab
        - For generic objects/furniture: First try PolyHaven, then Sketchfab
        - For custom or unique items not available in libraries: Use Hyper3D Rodin or Hunyuan3D
        - For environment lighting: Use PolyHaven HDRIs
        - For materials/textures: Use PolyHaven textures

    Only fall back to scripting when:
    - PolyHaven, Sketchfab, Poly Pizza, Hyper3D, and Hunyuan3D are all disabled
    - A simple primitive is explicitly requested
    - No suitable asset exists in any of the libraries
    - Hyper3D Rodin or Hunyuan3D failed to generate the desired asset
    - The task specifically requires a basic material/color

    **Best Practices:**
    - Always take a screenshot after completing a task to verify the visual result
    - Always call get_scene_info() after completing a task to verify the changes worked
    - When executing multiple operations, take intermediate screenshots to confirm each step
    - If something looks wrong in the screenshot or scene info, investigate and fix before proceeding
    """

# Main execution

def main():
    """Run the MCP server, or addon install CLI subcommands."""
    if len(sys.argv) > 1 and sys.argv[1] in {"install-addon", "addon-paths", "-h", "--help"}:
        code = run_addon_cli(sys.argv[1:])
        if code >= 0:
            raise SystemExit(code)

    # When run by hand (stdin is a TTY) the server appears to "hang" while it
    # silently waits for an MCP client; log a hint so that state is obvious.
    # Launched by a client, stdin is a pipe so this is skipped, and logging goes
    # to stderr, never to the stdio protocol on stdout.
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if interactive:
        logger.info(
            "BlenderMCP is an MCP server and is meant to be launched by your MCP "
            "client (Claude Desktop, Cursor, VS Code, ...), not run by hand. "
            "It will now wait silently for a client on stdin -- that is normal, "
            "not a hang. Press Ctrl-C to exit. "
            "Setup guide: https://github.com/ahujasid/blender-mcp#installation "
            "(if the addon is outdated this logs how to update it: uvx blender-mcp install-addon)"
        )
    mcp.run()

if __name__ == "__main__":
    main()
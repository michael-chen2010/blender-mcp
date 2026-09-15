"""Packaged entry point for isolated Blender asset preparation workers.

Task 1 intentionally ships only the worker bootstrap and capability metadata.
The observation/render/publish implementation is added by later pipeline tasks.
"""

from __future__ import annotations

import argparse
import json

SUPPORTED_PROFILES = ("PREVIEW", "METADATA", "PUBLISH")
WORKER_PROTOCOL_VERSION = 1


def build_status() -> dict[str, object]:
    return {
        "workerProtocolVersion": WORKER_PROTOCOL_VERSION,
        "supportedProfiles": list(SUPPORTED_PROFILES),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BlenderMCP asset preparation worker")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print worker capability metadata as JSON.",
    )
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(build_status(), sort_keys=True))
        return 0
    parser.error("asset preparation execution is not implemented yet; use --status")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

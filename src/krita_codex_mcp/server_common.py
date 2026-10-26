from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from .bridge_client import BridgeCallError
from .validation import InputError


async def tool_call(function: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    try:
        return await function()
    except (InputError, BridgeCallError) as exc:
        raise tool_error(exc) from exc


def image_result(metadata: dict[str, Any], png_base64: str) -> CallToolResult:
    # An explicit result avoids FastMCP also serializing the entire base64 image
    # into structuredContent. Keep the image first, as with Blender's look tool.
    return CallToolResult(
        content=[
            ImageContent(type="image", data=png_base64, mimeType="image/png"),
            TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
        ],
        structuredContent=metadata,
    )


def tool_error(exc: InputError | BridgeCallError) -> RuntimeError:
    if isinstance(exc, BridgeCallError):
        payload = {
            "code": exc.code,
            "message": exc.message,
            "details": dict(exc.details),
            "retryable": exc.retryable,
        }
    else:
        payload = {"code": "INVALID_ARGUMENT", "message": str(exc), "retryable": False}
    return RuntimeError(json.dumps(payload, ensure_ascii=False))

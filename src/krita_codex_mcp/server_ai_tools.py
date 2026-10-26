from __future__ import annotations

import json
from typing import Annotated, Literal

from mcp.types import CallToolResult, TextContent
from pydantic import Field

from .bridge_client import BridgeCallError
from .image_validation import validate_png_base64
from .server_common import image_result, tool_call, tool_error
from .validation import InputError


def register_ai_tools(mcp, service):
    async def call(command, action, **values):
        from krita_codex_protocol.ai import validate_ai

        try:
            args = validate_ai(
                {
                    "command": command,
                    "action": action,
                    **{k: v for k, v in values.items() if v is not None},
                }
            )
        except ValueError as exc:
            raise InputError(str(exc)) from exc
        try:
            return await service.client.execute("ai", args)
        except BridgeCallError as exc:
            if exc.code in {"UNSUPPORTED_OPERATION", "INVALID_REQUEST"}:
                raise BridgeCallError(
                    "AI_BRIDGE_UPDATE_REQUIRED", "Install the matching AI-enabled Krita bridge", {}
                ) from exc
            raise

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def krita_ai_status() -> dict:
        """Read optional AI plugin capabilities, server and connection state; no document needed."""
        return await tool_call(lambda: call("status", "read"))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def krita_ai_server(
        action: Literal["start", "stop", "connect", "disconnect", "refresh_models"],
        request_key: str,
    ) -> dict:
        """Schedule local managed AI server control. Poll operation_id with krita_ai_jobs."""
        return await tool_call(lambda: call("server", action, request_key=request_key))

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def krita_ai_catalog(
        kind: Literal["styles", "checkpoints", "loras", "upscalers"] = "styles",
        offset: Annotated[int, Field(ge=0, le=100000)] = 0,
        limit: Annotated[int, Field(ge=1, le=100)] = 50,
    ) -> dict:
        """List AI styles or server model identifiers. Does not download or install models."""
        return await tool_call(lambda: call("catalog", kind, offset=offset, limit=limit))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def krita_ai_generate(
        document_id: str,
        prompt: str,
        style_id: str,
        request_key: str,
        mode: Literal["generate", "refine", "inpaint"] = "generate",
        negative_prompt: str = "",
        strength: Annotated[float, Field(ge=0, le=1)] = 1.0,
        seed: Annotated[int, Field(ge=0, le=2**63 - 1)] = 0,
    ) -> dict:
        """Submit a local AI job for the explicit active document; never auto-apply results.

        Uses the current selection for inpainting. Returns operation_id immediately. Keep the
        same request_key when retrying; preview then apply_new_layers in separate calls.
        """
        return await tool_call(
            lambda: call(
                "generate",
                mode,
                document_id=document_id,
                prompt=prompt,
                style_id=style_id,
                request_key=request_key,
                negative_prompt=negative_prompt,
                strength=strength,
                seed=seed,
            )
        )

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": True})
    async def krita_ai_jobs(
        action: Literal["list", "status", "cancel"] = "list",
        operation_id: str | None = None,
    ) -> dict:
        """Read bounded MCP-owned job history or cancel a queued MCP job.

        Running server jobs cannot be interrupted in isolation and are not globally interrupted.
        """
        return await tool_call(lambda: call("jobs", action, operation_id=operation_id))

    @mcp.tool(annotations={"readOnlyHint": False, "openWorldHint": False})
    async def krita_ai_result(
        operation_id: str,
        action: Literal["read", "preview", "apply_new_layers"] = "read",
        result_index: Annotated[int, Field(ge=0, le=63)] = 0,
        document_id: str | None = None,
        request_key: str | None = None,
        max_dimension: Annotated[int, Field(ge=64, le=2048)] = 1024,
    ) -> CallToolResult:
        """Preview an AI result without editing, or apply it exclusively to NEW layers.

        Apply requires the original active document and request_key. Existing layers are never
        replaced. Changed input pixels/geometry are rejected. Reuse request_key on retry.
        """
        try:
            result = await call(
                "result",
                action,
                operation_id=operation_id,
                result_index=result_index,
                document_id=document_id,
                request_key=request_key,
                max_dimension=max_dimension,
            )
            if action == "preview":
                encoded = result.pop("png_base64", None)
                size = validate_png_base64(
                    encoded,
                    missing_message="Missing AI preview",
                    malformed_message="Invalid AI preview",
                )
                if size > service.config.preview_max_bytes:
                    raise InputError("AI preview exceeds byte limit")
                return image_result(result, encoded)
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result))],
                structuredContent=result,
            )
        except (InputError, BridgeCallError) as exc:
            raise tool_error(exc) from exc

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult
from pydantic import Field

from krita_codex_protocol import PathPolicyError

from .bridge_client import BridgeCallError
from .core_service import KritaService
from .server_common import image_result, tool_error
from .validation import InputError


def _write_png(path: Path, data: bytes, *, overwrite: bool) -> None:
    if not overwrite:
        # Exclusive creation also protects against a file appearing after validation.
        stream = path.open("xb")
        try:
            with stream:
                stream.write(data)
        except OSError:
            path.unlink(missing_ok=True)
            raise
        return
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".krita-preview-", delete=False
        ) as f:
            temporary = Path(f.name)
            f.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def register_preview_tools(mcp: FastMCP, service: KritaService) -> None:
    @mcp.tool(
        annotations={"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}
    )
    async def krita_preview_file(
        output_path: str,
        overwrite: bool = False,
        max_width: Annotated[int, Field(ge=64, le=4096)] = 1024,
        max_height: Annotated[int, Field(ge=64, le=4096)] = 1024,
        document_id: str | None = None,
        x: Annotated[int | None, Field(ge=0, le=1_000_000)] = None,
        y: Annotated[int | None, Field(ge=0, le=1_000_000)] = None,
        width: Annotated[int | None, Field(ge=1, le=32768)] = None,
        height: Annotated[int | None, Field(ge=1, le=32768)] = None,
        scale: Annotated[int, Field(ge=1, le=16)] = 1,
    ) -> CallToolResult:
        """Save a document preview PNG beneath an allowed root, without editing/saving the document.

        For clients that cannot display MCP images: embed preview_path with the client's
        local-file image syntax. The path is on the MCP server computer, not a public URL.
        """
        try:
            path = service.paths.validate_output(
                output_path, extensions={".png"}, overwrite=overwrite
            )
            metadata, png_base64 = await service.preview(
                max_width, max_height, document_id=document_id,
                x=x, y=y, width=width, height=height, scale=scale,
            )
            # Recheck the filesystem boundary after waiting for the bridge.
            path = service.paths.validate_output(
                str(path), extensions={".png"}, overwrite=overwrite
            )
            _write_png(path, base64.b64decode(png_base64, validate=True), overwrite=overwrite)
            return image_result({**metadata, "preview_path": str(path)}, png_base64)
        except (InputError, BridgeCallError) as exc:
            raise tool_error(exc) from exc
        except PathPolicyError as exc:
            raise tool_error(InputError(str(exc))) from exc
        except OSError as exc:
            raise tool_error(BridgeCallError("PREVIEW_WRITE_FAILED", str(exc), {})) from exc

from __future__ import annotations

from importlib.resources import files

from mcp.server.fastmcp import FastMCP

PREVIEW_URI = "ui://krita-codex-mcp/document-preview.html"
PREVIEW_MIME_TYPE = "text/html;profile=mcp-app"
PREVIEW_TOOL_META = {
    "ui": {"resourceUri": PREVIEW_URI, "visibility": ["model", "app"]},
    "openai/ui": {"entrypoints": [{"type": "thread"}]},
}


def register_preview_app(mcp: FastMCP) -> None:
    @mcp.resource(
        PREVIEW_URI,
        name="krita_document_preview",
        title="Krita document preview",
        mime_type=PREVIEW_MIME_TYPE,
        description="Read-only document image with a manual Refresh button.",
        meta={"ui": {"prefersBorder": True, "csp": {
            "connectDomains": [], "resourceDomains": [],
        }}, "openai/ui": {
            "preferredDisplayMode": "fullscreen",
            "availableDisplayModes": ["inline", "fullscreen"],
        }},
    )
    def document_preview() -> str:
        return files("krita_codex_mcp").joinpath("preview.html").read_text(encoding="utf-8")

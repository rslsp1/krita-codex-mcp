from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.shared.memory import create_connected_server_and_client_session

from krita_codex_mcp.bridge_client import BridgeCallError
from krita_codex_mcp.preview_app import PREVIEW_MIME_TYPE, PREVIEW_URI
from krita_codex_mcp.server import build_server
from krita_codex_mcp.server_preview_tools import _write_png
from krita_codex_protocol import BridgeConfig

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg=="
)


class PreviewBridge:
    def __init__(self) -> None:
        self.calls = []
        self.error = None
        self.png = base64.b64encode(PNG).decode("ascii")

    async def execute(self, operation, arguments):
        self.calls.append((operation, arguments))
        if self.error:
            raise self.error
        if operation != "preview":
            raise AssertionError(f"Preview must not edit or save the document: {operation}")
        return {
            "document_id": "doc-1", "document_name": "Unbenannt",
            "width": 1, "height": 1, "png_base64": self.png,
        }


class PreviewDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bridge = PreviewBridge()
        config = BridgeConfig.from_mapping({"token": "x" * 32, "allowed_roots": [str(self.root)]})
        self.server = build_server(config, self.bridge)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_wire_discovery_and_preview_work_without_app_capability(self):
        async with create_connected_server_and_client_session(self.server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            self.assertEqual(tools["krita_preview"].meta["ui"]["resourceUri"], PREVIEW_URI)
            self.assertEqual(tools["krita_preview"].meta["ui"]["visibility"], ["model", "app"])
            self.assertEqual(tools["krita_preview"].title, "Krita document preview")
            self.assertEqual(
                tools["krita_preview"].meta["openai/ui"]["entrypoints"], [{"type": "thread"}]
            )
            self.assertTrue(tools["krita_preview"].annotations.readOnlyHint)
            self.assertFalse(tools["krita_preview_file"].annotations.readOnlyHint)
            resources = (await client.list_resources()).resources
            resource = next(r for r in resources if str(r.uri) == PREVIEW_URI)
            self.assertEqual(resource.mimeType, PREVIEW_MIME_TYPE)
            self.assertEqual(resource.meta["ui"]["csp"]["connectDomains"], [])
            self.assertEqual(resource.meta["openai/ui"]["preferredDisplayMode"], "fullscreen")
            html = (await client.read_resource(PREVIEW_URI)).contents[0]
            self.assertEqual(html.mimeType, PREVIEW_MIME_TYPE)
            self.assertIn("<title>Krita document preview</title>", html.text)
            self.assertFalse(self.bridge.calls)  # Reading UI HTML needs no running Krita.
            result = await client.call_tool("krita_preview", {"document_id": "doc-1"})
            self.assertFalse(result.isError)
            self.assertEqual(result.content[0].type, "image")
            self.assertEqual(base64.b64decode(result.content[0].data), PNG)
            self.assertEqual(result.structuredContent["document_name"], "Unbenannt")
            self.assertNotIn(self.bridge.png, str(result.structuredContent))
            self.assertNotIn(self.bridge.png, result.content[1].text)

    async def test_file_preview_preserves_region_and_document_target(self):
        path = self.root / "preview.png"
        async with create_connected_server_and_client_session(self.server) as client:
            result = await client.call_tool("krita_preview_file", {
                "output_path": str(path), "document_id": "inactive-doc", "max_width": 64,
                "max_height": 64, "x": 1, "y": 2, "width": 3, "height": 4, "scale": 2,
            })
        self.assertFalse(result.isError)
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(result.structuredContent["preview_path"], str(path.resolve()))
        self.assertEqual(self.bridge.calls, [("preview", {
            "document_id": "inactive-doc", "max_width": 64, "max_height": 64,
            "region": {"x": 1, "y": 2, "width": 3, "height": 4}, "scale": 2,
        })])

    async def test_file_preview_rejects_invalid_paths_before_contacting_krita(self):
        existing = self.root / "existing.png"
        existing.write_bytes(b"keep")
        with tempfile.TemporaryDirectory() as outside:
            paths = ["relative.png", str(Path(outside) / "outside.png"),
                     str(self.root / "wrong.jpg"), str(self.root / "missing" / "preview.png"),
                     str(existing)]
            async with create_connected_server_and_client_session(self.server) as client:
                for path in paths:
                    with self.subTest(path=path):
                        result = await client.call_tool("krita_preview_file", {"output_path": path})
                        self.assertTrue(result.isError)
        self.assertFalse(self.bridge.calls)
        self.assertEqual(existing.read_bytes(), b"keep")

    async def test_failed_bridge_and_invalid_png_leave_existing_file_intact(self):
        path = self.root / "preview.png"
        path.write_bytes(b"keep")
        async with create_connected_server_and_client_session(self.server) as client:
            self.bridge.error = BridgeCallError("NO_ACTIVE_DOCUMENT", "No active document", {})
            result = await client.call_tool("krita_preview_file", {
                "output_path": str(path), "overwrite": True,
            })
            self.assertTrue(result.isError)
            self.assertIn("NO_ACTIVE_DOCUMENT", result.content[0].text)
            self.assertEqual(path.read_bytes(), b"keep")
            self.bridge.error = None
            self.bridge.png = base64.b64encode(b"not a PNG").decode("ascii")
            result = await client.call_tool("krita_preview_file", {
                "output_path": str(path), "overwrite": True,
            })
            self.assertTrue(result.isError)
            self.assertEqual(path.read_bytes(), b"keep")
        self.assertEqual(list(self.root.iterdir()), [path])

    async def test_explicit_overwrite_and_write_failure(self):
        path = self.root / "preview.png"
        path.write_bytes(b"keep")
        async with create_connected_server_and_client_session(self.server) as client:
            with patch(
                "krita_codex_mcp.server_preview_tools.os.replace", side_effect=OSError("full")
            ):
                result = await client.call_tool("krita_preview_file", {
                    "output_path": str(path), "overwrite": True,
                })
            self.assertTrue(result.isError)
            self.assertIn("PREVIEW_WRITE_FAILED", result.content[0].text)
            self.assertEqual(path.read_bytes(), b"keep")
            self.assertEqual(list(self.root.iterdir()), [path])
            result = await client.call_tool("krita_preview_file", {
                "output_path": str(path), "overwrite": True,
            })
        self.assertFalse(result.isError)
        self.assertEqual(path.read_bytes(), PNG)

    def test_exclusive_write_does_not_clobber_a_file_created_after_validation(self):
        path = self.root / "preview.png"
        path.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            _write_png(path, PNG, overwrite=False)
        self.assertEqual(path.read_bytes(), b"keep")

    async def test_symlink_cannot_escape_allowed_root(self):
        with tempfile.TemporaryDirectory() as outside:
            link = self.root / "elsewhere"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Directory symlinks unavailable")
            async with create_connected_server_and_client_session(self.server) as client:
                result = await client.call_tool("krita_preview_file", {
                    "output_path": str(link / "preview.png"), "overwrite": True,
                })
            self.assertTrue(result.isError)
            self.assertFalse(self.bridge.calls)
            self.assertFalse(list(Path(outside).iterdir()))

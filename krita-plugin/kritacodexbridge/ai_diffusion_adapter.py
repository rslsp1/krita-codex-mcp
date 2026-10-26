"""Version-bound adapter for the already loaded AI Diffusion 1.53.0 plugin."""

from __future__ import annotations

import base64
import hashlib
import sys
from dataclasses import replace

from .ai_controller import fail


class AIDiffusionAdapter:
    def __init__(self, service):
        self.service = service

    def require(self):
        plugin = sys.modules.get("ai_diffusion")
        if plugin is None:
            fail("AI_PLUGIN_UNAVAILABLE", "Enable AI Diffusion in Krita and restart Krita")
        if getattr(plugin, "__version__", None) != "1.53.0":
            fail("AI_PLUGIN_UNSUPPORTED", "This adapter supports AI Diffusion 1.53.0")
        module = sys.modules.get("ai_diffusion.model.root")
        if module is None or not hasattr(module.root, "_connection"):
            fail("AI_PLUGIN_NOT_READY", "AI Diffusion has not finished initialization")
        return module.root

    def status(self):
        plugin = sys.modules.get("ai_diffusion")
        result = {
            "plugin_available": plugin is not None,
            "plugin_version": getattr(plugin, "__version__", None),
            "supported_version": "1.53.0",
            "capabilities": [],
            "new_layers_only": True,
        }
        try:
            root = self.require()
        except Exception as exc:
            result["adapter_error"] = getattr(exc, "code", "AI_PLUGIN_NOT_READY")
            return result
        result.update(
            server_state=root.server.state.name,
            connection_state=root.connection.state.name,
            capabilities=[
                "local_server",
                "catalog",
                "generate",
                "refine",
                "inpaint",
                "jobs",
                "preview",
                "apply_new_layers",
            ],
            connection_has_error=bool(root.connection.error),
        )
        return result

    def client(self):
        from ai_diffusion.settings import ServerMode, settings

        root = self.require()
        if settings.server_mode is not ServerMode.managed:
            fail("AI_SERVER_MODE_UNSUPPORTED", "First version supports local managed servers only")
        client = root.connection.client_if_connected
        if client is None or root.connection.state.name != "connected":
            fail("AI_NOT_CONNECTED", "Start/connect the local server first")
        return client

    def schedule(self, coroutine):
        from ai_diffusion import eventloop

        return eventloop.run(coroutine)

    def listen(self, callback):
        self.require().connection.message_received.connect(callback)

    def check_server_action(self, action):
        from ai_diffusion.settings import ServerMode, settings

        root = self.require()
        if settings.server_mode is not ServerMode.managed:
            fail(
                "AI_SERVER_MODE_UNSUPPORTED",
                "Only the plugin-managed local server can be controlled",
            )
        if action in {"stop", "disconnect"} and any(
            m.jobs.any_executing() or m.live.is_active or m.custom.is_live for m in root.models
        ):
            fail("AI_BUSY", "Plugin documents still have active jobs")
        if root.server.state.name in {"installing", "starting", "stopping"}:
            fail("AI_BUSY", "Server is already changing state")

    async def server(self, action):
        from ai_diffusion.backend.comfy_client import ComfyClient

        root = self.require()
        self.check_server_action(action)
        if action == "start":
            if root.server.state.name != "running":
                await root.server.start()
            await root.connection._connect(ComfyClient(root.server.url))
        elif action == "stop":
            await root.connection.disconnect()
            await root.server.stop()
        elif action == "connect":
            if root.server.state.name != "running":
                fail("AI_SERVER_NOT_RUNNING", "Start the managed server first")
            await root.connection._connect(ComfyClient(root.server.url))
        elif action == "disconnect":
            await root.connection.disconnect()
        elif action == "refresh_models":
            await self.client().refresh()
            root.connection.models_changed.emit()
        if action in {"start", "connect"} and root.connection.state.name != "connected":
            fail("AI_CONNECT_FAILED", "Plugin could not connect; inspect its connection panel")

    def catalog(self, kind, offset, limit):
        from ai_diffusion.style import Styles, filter_supported_styles

        client = self.client()
        if kind == "styles":
            items = [
                {"id": s.filename, "name": s.name}
                for s in filter_supported_styles(Styles.list().filtered(True), client)
            ]
        else:
            items = [{"id": name} for name in sorted(getattr(client.models, kind))]
        return {"items": items[offset : offset + limit], "total": len(items), "offset": offset}

    def document(self, document_id):
        # Never silently activate a different view or fall back to another document.
        doc = self.service._target_document({"document_id": document_id})
        if not self.service._same_document(doc, self.service._active_document()):
            fail("AI_DOCUMENT_NOT_ACTIVE", "Activate the requested document before AI operations")
        return doc

    def fingerprint(self, doc):
        if doc.width() * doc.height() > self.service.config.document_max_pixels:
            fail("AI_DOCUMENT_TOO_LARGE", "Document exceeds configured pixel limit")
        doc.waitForDone()
        digest = hashlib.sha256()
        digest.update(
            str(
                (doc.width(), doc.height(), doc.colorModel(), doc.colorDepth(), doc.colorProfile())
            ).encode()
        )
        digest.update(bytes(doc.pixelData(0, 0, doc.width(), doc.height())))
        return digest.hexdigest()

    def prepare(self, args):
        from ai_diffusion.backend.api import WorkflowKind
        from ai_diffusion.style import Styles, filter_supported_styles

        client = self.client()
        doc = self.document(args["document_id"])
        fingerprint = self.fingerprint(doc)
        model = self.require().model_for_active_document()
        if model.live.is_active or model.custom.is_live:
            fail("AI_BUSY", "Stop live generation before submitting an MCP job")
        styles = {
            s.filename: s for s in filter_supported_styles(Styles.list().filtered(True), client)
        }
        if args["style_id"] not in styles:
            fail("AI_STYLE_UNAVAILABLE", "Style is missing or incompatible with server models")
        ok, _ = model.document.check_color_mode()
        if not ok:
            fail("AI_COLOR_MODE_UNSUPPORTED", "AI requires an RGBA/U8 document")
        if model.edit_mode or model.active_regions is not model.regions:
            fail("AI_WORKFLOW_UNSUPPORTED", "Turn off edit mode before this operation")
        if not model.regions.get_active_region_layer(use_parent=True).is_root:
            fail("AI_REGION_UNSUPPORTED", "First version uses selections, not linked regions")
        if model.regions._regions:
            fail("AI_REGION_UNSUPPORTED", "Use a document without linked AI regions")
        snapshot = {k: getattr(model, k) for k in ("style", "strength", "seed", "fixed_seed")}
        regions = model.regions
        prompts = regions.positive, regions.negative, regions.negative_enabled
        edit_mode = model.edit_mode
        # Capture inputs synchronously on Qt's main thread, then restore UI parameters before await.
        try:
            model.style = styles[args["style_id"]]
            model.strength, model.seed, model.fixed_seed = args["strength"], args["seed"], True
            model.edit_mode = False
            regions.positive, regions.negative = args["prompt"], args.get("negative_prompt", "")
            regions.negative_enabled = True
            workflow, params, _ = model._prepare_workflow()
            masked = workflow.kind in {WorkflowKind.inpaint, WorkflowKind.refine_region}
            if args["action"] == "inpaint" and not masked:
                fail("AI_SELECTION_REQUIRED", "Inpainting requires a selection or active region")
            if args["action"] != "inpaint" and masked:
                fail("AI_SELECTION_PRESENT", "Use inpaint or clear the current selection/region")
            if params.is_layered or params.resize_canvas:
                fail("AI_WORKFLOW_UNSUPPORTED", "Layered/resize workflows are not supported yet")
        finally:
            for key, value in snapshot.items():
                setattr(model, key, value)
            model.edit_mode = edit_mode
            regions.positive, regions.negative, regions.negative_enabled = prompts
        return {
            "document_id": args["document_id"],
            "model": model,
            "workflow": workflow,
            "params": params,
            "client": client,
            "fingerprint": fingerprint,
        }

    async def submit(self, prepared):
        # Separate MCP-owned jobs never enter DocumentModel.jobs. Thus plugin auto-apply and
        # generation_finished_action cannot mutate source layers before explicit MCP apply.
        return await prepared["client"].enqueue(prepared["workflow"], front=False)

    async def cancel(self, job_id, prepared):
        await prepared["client"].cancel([job_id])

    def preview(self, image, maximum):
        from ai_diffusion.image import Extent, Image

        if max(image.width, image.height) > maximum:
            image = Image.scale_to_fit(image, Extent(maximum, maximum))
        payload = bytes(image.to_bytes())
        if len(payload) > self.service.config.preview_max_bytes:
            fail("PREVIEW_TOO_LARGE", "AI preview exceeds configured byte limit")
        return {
            "png_base64": base64.b64encode(payload).decode("ascii"),
            "preview_width": image.width,
            "preview_height": image.height,
        }

    def apply(self, prepared, image):
        from ai_diffusion.settings import ApplyBehavior, ApplyRegionBehavior

        doc = self.document(prepared["document_id"])
        if self.fingerprint(doc) != prepared["fingerprint"]:
            fail("DOCUMENT_CHANGED", "Input pixels or document geometry changed; generate again")
        params = prepared["params"]
        if image.width != params.bounds.width or image.height != params.bounds.height:
            fail("AI_GEOMETRY_MISMATCH", "Result geometry differs from captured target bounds")
        before = {self.service._node_id(n) for n in doc.rootNode().childNodes()}
        # Flatten region results onto a NEW top-level layer; never hide or modify region layers.
        prepared["model"].apply_result(
            image,
            replace(params, regions=[], resize_canvas=False),
            behavior=ApplyBehavior.layer,
            region_behavior=ApplyRegionBehavior.none,
            prefix="[MCP AI] ",
        )
        doc.waitForDone()
        doc.refreshProjection()
        after = {self.service._node_id(n) for n in doc.rootNode().childNodes()}
        created = sorted(after - before)
        if not created:
            fail("AI_APPLY_FAILED", "Krita did not create a result layer")
        self.service.bridge_revision += 1
        # Allow another result from the same operation on top of already applied new layers.
        prepared["fingerprint"] = self.fingerprint(doc)
        return {"new_layer_ids": created, "new_layers_only": True}

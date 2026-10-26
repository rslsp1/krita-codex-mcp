from __future__ import annotations

import asyncio
import importlib
import sys
import types
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from krita_codex_protocol.ai import validate_ai

ROOT = Path(__file__).resolve().parents[1] / "krita-plugin" / "kritacodexbridge"
PACKAGE = "_krita_ai_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules.setdefault(PACKAGE, package)
controller_module = importlib.import_module(f"{PACKAGE}.ai_controller")
adapter_module = importlib.import_module(f"{PACKAGE}.ai_diffusion_adapter")
errors = importlib.import_module(f"{PACKAGE}.errors")
AIController = controller_module.AIController
AIDiffusionAdapter = adapter_module.AIDiffusionAdapter


class FakeAdapter:
    def __init__(self):
        self.tasks = []
        self.submissions = 0
        self.applications = 0
        self.cancelled = []
        self.document_changed = False

    def require(self):
        return self

    def listen(self, callback):
        self.callback = callback

    def status(self):
        return {"plugin_available": True}

    def prepare(self, args):
        return dict(args)

    def schedule(self, coroutine):
        self.tasks.append(asyncio.create_task(coroutine))

    async def submit(self, prepared):
        self.submissions += 1
        return f"job-{self.submissions}"

    async def cancel(self, job_id, prepared):
        self.cancelled.append(job_id)

    def apply(self, prepared, image):
        if self.document_changed:
            raise errors.KritaOperationError("DOCUMENT_CHANGED", "Changed")
        self.applications += 1
        return {"new_layer_ids": [f"new-{self.applications}"]}

    def preview(self, image, maximum):
        return {"png_base64": "png"}

    def check_server_action(self, action):
        pass

    async def server(self, action):
        pass


def generation(**overrides):
    return (
        dict(
            command="generate",
            action="generate",
            document_id="doc-1",
            request_key="g1",
            prompt="red eyes",
            style_id="style.json",
            strength=1,
            seed=7,
        )
        | overrides
    )


def message(event="finished", job="job-1", images=None):
    return types.SimpleNamespace(
        event=types.SimpleNamespace(name=event), job_id=job, progress=0.5, images=images, error=None
    )


def image(width=64, height=64):
    return types.SimpleNamespace(width=width, height=height)


class AIControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = FakeAdapter()
        self.controller = AIController(self.adapter)

    async def asyncTearDown(self):
        await asyncio.gather(*self.adapter.tasks)

    async def completed(self):
        op = self.controller.execute(generation())
        await asyncio.gather(*self.adapter.tasks)
        self.controller.message(message(images=[image()]))
        return op["operation_id"]

    def result(self, op, **kw):
        return self.controller.execute(
            dict(
                command="result",
                action="apply_new_layers",
                operation_id=op,
                document_id="doc-1",
                request_key="apply-1",
            )
            | kw
        )

    async def test_retry_submits_only_once_and_rejects_changed_request(self):
        first = self.controller.execute(generation())
        second = self.controller.execute(generation())
        self.assertEqual(first["operation_id"], second["operation_id"])
        await asyncio.gather(*self.adapter.tasks)
        self.assertEqual(self.adapter.submissions, 1)
        with self.assertRaisesRegex(errors.KritaOperationError, "different arguments"):
            self.controller.execute(generation(prompt="different"))

    async def test_result_does_not_apply_until_explicit_request_and_retry_is_safe(self):
        op = await self.completed()
        self.result(op, action="preview")
        self.assertEqual(self.adapter.applications, 0)
        first = self.result(op)
        self.assertEqual(first, self.result(op))
        self.assertEqual(self.adapter.applications, 1)

    async def test_partial_application_failure_reserves_retry_key(self):
        op = await self.completed()

        def broken(prepared, image):
            self.adapter.applications += 1
            raise RuntimeError("failure after mutation")

        self.adapter.apply = broken
        with self.assertRaises(RuntimeError):
            self.result(op)
        with self.assertRaisesRegex(errors.KritaOperationError, "Previous apply failed"):
            self.result(op)
        self.assertEqual(self.adapter.applications, 1)

    async def test_changed_or_wrong_document_is_rejected(self):
        op = await self.completed()
        with self.assertRaises(errors.KritaOperationError):
            self.result(op, document_id="another")
        self.adapter.document_changed = True
        with self.assertRaisesRegex(errors.KritaOperationError, "Changed"):
            self.result(op)
        self.assertEqual(self.adapter.applications, 0)

    async def test_running_job_never_causes_global_interrupt(self):
        op = self.controller.execute(generation())["operation_id"]
        await asyncio.gather(*self.adapter.tasks)
        self.controller.message(message(event="progress"))
        with self.assertRaises(errors.KritaOperationError):
            self.controller.execute(dict(command="jobs", action="cancel", operation_id=op))
        self.assertEqual(self.adapter.cancelled, [])

    async def test_queued_cancel_only_targets_owned_job(self):
        op = self.controller.execute(generation())["operation_id"]
        await asyncio.gather(*self.adapter.tasks)
        self.controller.execute(dict(command="jobs", action="cancel", operation_id=op))
        await asyncio.gather(*self.adapter.tasks)
        self.assertEqual(self.adapter.cancelled, ["job-1"])

    async def test_foreign_events_and_oversized_results(self):
        op = self.controller.execute(generation())["operation_id"]
        await asyncio.gather(*self.adapter.tasks)
        self.controller.message(message(job="foreign", images=[image()]))
        self.assertEqual(self.controller.operations[op]["state"], "queued")
        self.controller.message(message(images=[image(20000, 20000)]))
        self.assertEqual(self.controller.operations[op]["state"], "failed")
        self.assertEqual(self.controller.operations[op]["results"], [])

    async def test_unknown_session_and_server_busy(self):
        self.controller.execute(generation())
        with self.assertRaises(errors.KritaOperationError):
            self.controller.execute(dict(command="server", action="stop", request_key="s1"))
        with self.assertRaises(errors.KritaOperationError):
            self.controller.execute(dict(command="jobs", action="status", operation_id="old"))

    async def test_server_failure_is_recorded_without_sensitive_exception_string(self):
        async def broken(action):
            raise RuntimeError("secret credential")

        self.adapter.server = broken
        op = self.controller.execute(dict(command="server", action="start", request_key="s"))
        await asyncio.gather(*self.adapter.tasks)
        result = self.controller.execute(
            dict(command="jobs", action="status", operation_id=op["operation_id"])
        )
        self.assertEqual(result["state"], "failed")
        self.assertNotIn("secret", str(result))


class AIValidationTests(unittest.TestCase):
    def test_replacement_code_nan_and_unknown_fields_rejected(self):
        for values in (
            {"replace": True},
            {"code": "evil"},
            {"strength": float("nan")},
            {"seed": True},
            {"prompt": "x" * 8193},
        ):
            with self.subTest(values=list(values)), self.assertRaises(ValueError):
                validate_ai(generation(**values))

    def test_missing_document_and_apply_key(self):
        args = generation()
        del args["document_id"]
        with self.assertRaises(ValueError):
            validate_ai(args)
        with self.assertRaises(ValueError):
            validate_ai(dict(command="result", action="apply_new_layers", operation_id="op"))

    def test_absent_plugin_is_optional(self):
        with patch.dict(sys.modules, {"ai_diffusion": None}):
            status = AIDiffusionAdapter(None).status()
        self.assertFalse(status["plugin_available"])
        self.assertTrue(status["new_layers_only"])


@dataclass
class Params:
    regions: list
    resize_canvas: bool
    bounds: object


class AIApplyTests(unittest.TestCase):
    def test_global_replace_settings_are_never_consulted_and_original_layers_survive(self):
        settings = types.ModuleType("ai_diffusion.settings")
        settings.ApplyBehavior = types.SimpleNamespace(layer="NEW", replace="REPLACE")
        settings.ApplyRegionBehavior = types.SimpleNamespace(none="NONE", replace="REPLACE")
        settings.settings = types.SimpleNamespace(apply_behavior="REPLACE")
        nodes = ["original"]
        doc = types.SimpleNamespace(
            rootNode=lambda: types.SimpleNamespace(childNodes=lambda: nodes),
            waitForDone=lambda: None,
            refreshProjection=lambda: None,
        )
        service = types.SimpleNamespace(_node_id=lambda n: n, bridge_revision=0)
        adapter = AIDiffusionAdapter(service)
        adapter.document = lambda doc_id: doc
        adapter.fingerprint = lambda doc: "unchanged"
        calls = []

        def apply(img, params, **kw):
            calls.append((params, kw))
            nodes.append("new-result")

        model = types.SimpleNamespace(apply_result=apply)
        original_params = Params(["region"], True, types.SimpleNamespace(width=64, height=64))
        prepared = dict(
            document_id="doc", model=model, params=original_params, fingerprint="unchanged"
        )
        with patch.dict(sys.modules, {"ai_diffusion.settings": settings}):
            result = adapter.apply(prepared, image())
        self.assertEqual(nodes, ["original", "new-result"])
        self.assertEqual(result["new_layer_ids"], ["new-result"])
        self.assertEqual(calls[0][1]["behavior"], "NEW")
        self.assertEqual(calls[0][1]["region_behavior"], "NONE")
        self.assertFalse(calls[0][0].resize_canvas)
        self.assertEqual(calls[0][0].regions, [])
        self.assertTrue(original_params.resize_canvas)
        self.assertEqual(settings.settings.apply_behavior, "REPLACE")


class AIPrepareTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_restores_ui_and_submission_bypasses_plugin_auto_apply(self):
        from enum import Enum

        class Kind(Enum):
            generate = 0
            inpaint = 1
            refine_region = 2

        api = types.ModuleType("ai_diffusion.backend.api")
        api.WorkflowKind = Kind
        style_module = types.ModuleType("ai_diffusion.style")
        old_style = types.SimpleNamespace(filename="old")
        new_style = types.SimpleNamespace(filename="new")
        style_module.Styles = types.SimpleNamespace(
            list=lambda: types.SimpleNamespace(filtered=lambda show: [new_style])
        )
        style_module.filter_supported_styles = lambda styles, client: styles
        regions = types.SimpleNamespace(
            positive="original",
            negative="negative",
            negative_enabled=False,
            _regions=[],
            get_active_region_layer=lambda **kw: types.SimpleNamespace(is_root=True),
        )
        workflow = types.SimpleNamespace(kind=Kind.generate)
        params = types.SimpleNamespace(is_layered=False, resize_canvas=False)
        model = types.SimpleNamespace(
            style=old_style,
            strength=0.2,
            seed=2,
            fixed_seed=False,
            regions=regions,
            active_regions=regions,
            edit_mode=False,
            live=types.SimpleNamespace(is_active=False),
            custom=types.SimpleNamespace(is_live=False),
            document=types.SimpleNamespace(check_color_mode=lambda: (True, "")),
        )
        captured = []

        def prepare():
            captured.append((model.style, regions.positive, model.seed, model.fixed_seed))
            return workflow, params, None

        model._prepare_workflow = prepare
        submitted = []

        async def enqueue(value, front):
            submitted.append(value)
            return "owned-job"

        client = types.SimpleNamespace(enqueue=enqueue)
        adapter = AIDiffusionAdapter(None)
        adapter.client = lambda: client
        adapter.document = lambda doc_id: object()
        adapter.fingerprint = lambda doc: "digest"
        adapter.require = lambda: types.SimpleNamespace(model_for_active_document=lambda: model)
        with patch.dict(
            sys.modules, {"ai_diffusion.backend.api": api, "ai_diffusion.style": style_module}
        ):
            prepared = adapter.prepare(generation(style_id="new"))
            self.assertEqual(await adapter.submit(prepared), "owned-job")
            self.assertEqual(submitted, [workflow])
            self.assertEqual(captured, [(new_style, "red eyes", 7, True)])
            self.assertIs(model.style, old_style)
            self.assertEqual(
                (regions.positive, regions.negative, regions.negative_enabled),
                ("original", "negative", False),
            )
            self.assertEqual((model.strength, model.seed, model.fixed_seed), (0.2, 2, False))
            with self.assertRaises(errors.KritaOperationError):
                adapter.prepare(generation(style_id="new", action="inpaint"))
            self.assertIs(model.style, old_style)
            self.assertEqual(regions.positive, "original")


if __name__ == "__main__":
    unittest.main()

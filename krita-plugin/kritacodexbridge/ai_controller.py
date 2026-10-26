"""Bounded, session-local AI operations. No Qt or optional-plugin imports at module load."""

from __future__ import annotations

import json
from collections import OrderedDict
from uuid import uuid4

from krita_codex_protocol.ai import validate_ai

from .errors import KritaOperationError

TERMINAL = {"succeeded", "failed", "cancelled"}


def fail(code, message):
    raise KritaOperationError(code, message)


class AIController:
    def __init__(self, adapter):
        self.adapter = adapter
        self.operations = OrderedDict()
        self.keys = {}
        self.pending_messages = OrderedDict()
        self.listening = False

    def execute(self, raw):
        try:
            args = validate_ai(raw)
        except ValueError as exc:
            fail("INVALID_ARGUMENT", str(exc))
        command, action = args["command"], args["action"]
        if command == "status":
            return self.adapter.status()
        self.adapter.require()
        if command == "catalog":
            return self.adapter.catalog(action, args.get("offset", 0), args.get("limit", 50))
        if command in {"generate", "server"}:
            return self.start(args)
        if command == "jobs" and action == "list":
            return {"operations": [self.public(op) for op in self.operations.values()]}
        op = self.operations.get(args["operation_id"])
        if op is None:
            fail("AI_OPERATION_NOT_FOUND", "Operation expired or belongs to another Krita session")
        if command == "jobs":
            if action == "cancel":
                self.cancel(op)
            return self.public(op)
        return self.result(op, args)

    def start(self, args):
        key = args["request_key"]
        fingerprint = json.dumps(args, sort_keys=True, separators=(",", ":"))
        if key in self.keys:
            old_fingerprint, op_id = self.keys[key]
            if old_fingerprint != fingerprint:
                fail("AI_REQUEST_CONFLICT", "request_key was already used with different arguments")
            return self.public(self.operations[op_id])
        if len(self.operations) >= 32:
            # Do not forget idempotency keys and accidentally submit a repeated job.
            fail(
                "AI_HISTORY_FULL", "Session operation limit reached; restart Krita to reset history"
            )
        if args["command"] == "server" and any(
            op["state"] not in TERMINAL for op in self.operations.values()
        ):
            fail("AI_BUSY", "Wait for active MCP operations before changing server state")
        if args["command"] == "generate" and any(
            op["kind"] == "server" and op["state"] not in TERMINAL
            for op in self.operations.values()
        ):
            fail("AI_BUSY", "Wait for the server operation to finish")
        op = {
            "operation_id": str(uuid4()),
            "kind": args["command"],
            "state": "preparing",
            "job_id": None,
            "progress": 0.0,
            "error": None,
            "results": [],
            "applied": {},
            "document_id": args.get("document_id"),
        }
        self.operations[op["operation_id"]] = op
        self.keys[key] = (fingerprint, op["operation_id"])
        try:
            if args["command"] == "generate":
                if not self.listening:
                    self.adapter.listen(self.message)
                    self.listening = True
                op["prepared"] = self.adapter.prepare(args)
                self.adapter.schedule(self.submit(op))
            else:
                self.adapter.check_server_action(args["action"])
                self.adapter.schedule(self.server(op, args["action"]))
        except Exception as exc:
            op["state"], op["error"] = "failed", self.error(exc)
        return self.public(op)

    async def server(self, op, action):
        try:
            op["state"] = "running"
            await self.adapter.server(action)
            op["state"] = "succeeded"
        except Exception as exc:
            op["state"], op["error"] = "failed", self.error(exc)

    async def submit(self, op):
        try:
            op["state"] = "submitting"
            op["job_id"] = await self.adapter.submit(op["prepared"])
            op["state"] = "queued"
            message = self.pending_messages.pop(op["job_id"], None)
            if message is not None:
                self.message(message)
        except Exception as exc:
            op["state"], op["error"] = "failed", self.error(exc)

    def message(self, message):
        event = message.event.name
        if event == "disconnected":
            for op in self.operations.values():
                if op["kind"] == "generate" and op["state"] not in TERMINAL:
                    op["state"] = "failed"
                    op["error"] = {"code": "AI_CONNECTION_LOST", "message": "Server disconnected"}
            return
        op = next(
            (
                o
                for o in self.operations.values()
                if o["job_id"] is not None and o["job_id"] == message.job_id
            ),
            None,
        )
        if op is None:
            if any(o["state"] == "submitting" for o in self.operations.values()):
                self.pending_messages[message.job_id] = message
                while len(self.pending_messages) > 64:
                    self.pending_messages.popitem(last=False)
            return
        if op["state"] in TERMINAL:
            return
        if event in {"progress", "upload", "queued"}:
            op["state"] = "running"
            op["progress"] = max(0.0, min(1.0, message.progress))
        elif event == "finished":
            images = list(message.images) if message.images else []
            byte_count = sum(i.width * i.height * 4 for i in images)
            retained = sum(o.get("result_bytes", 0) for o in self.operations.values())
            if not images or len(images) > 64 or byte_count + retained > 128 * 1024**2:
                op["state"] = "failed"
                op["error"] = {"code": "AI_RESULT_LIMIT", "message": "Empty or oversized AI result"}
            else:
                op["results"], op["result_bytes"] = images, byte_count
                op["state"], op["progress"] = "succeeded", 1.0
        elif event in {"error", "payment_required", "interrupted"}:
            op["state"] = "cancelled" if event == "interrupted" else "failed"
            op["error"] = {"code": "AI_JOB_FAILED", "message": "Server job ended: " + event}

    def cancel(self, op):
        if op["state"] in TERMINAL:
            return
        if op["kind"] != "generate" or op["state"] != "queued":
            fail(
                "AI_CANCEL_UNSAFE",
                "Only queued owned jobs can be cancelled without global interrupt",
            )
        op["state"] = "cancelling"
        self.adapter.schedule(self.cancel_queued(op))

    async def cancel_queued(self, op):
        try:
            await self.adapter.cancel(op["job_id"], op["prepared"])
            if op["state"] not in TERMINAL:
                op["state"] = "cancelled"
        except Exception as exc:
            op["state"], op["error"] = "failed", self.error(exc)

    def result(self, op, args):
        if op["kind"] != "generate" or op["state"] != "succeeded":
            fail("AI_RESULT_NOT_READY", "Generation has no completed result")
        index = args.get("result_index", 0)
        if index >= len(op["results"]):
            fail("INVALID_ARGUMENT", "result_index is out of range")
        image = op["results"][index]
        metadata = {
            **self.public(op),
            "result_index": index,
            "width": image.width,
            "height": image.height,
        }
        if args["action"] == "preview":
            return {**metadata, **self.adapter.preview(image, args.get("max_dimension", 1024))}
        if args["action"] == "apply_new_layers":
            if args["document_id"] != op["document_id"]:
                fail("AI_DOCUMENT_MISMATCH", "Use the original document_id")
            key = args["request_key"]
            if key in op["applied"]:
                old_index, response = op["applied"][key]
                if index != old_index:
                    fail("AI_REQUEST_CONFLICT", "Apply key already belongs to a different result")
                if response is None:
                    fail(
                        "AI_APPLY_UNCERTAIN",
                        "Previous apply failed; inspect layers before a new request",
                    )
                return response
            if len(op["applied"]) >= 16:
                fail("AI_RESULT_LIMIT", "At most 16 applications per operation")
            # Reserve BEFORE the first possible mutation. An exception after creating the layer
            # must never turn a transport retry into a duplicate application.
            op["applied"][key] = (index, None)
            response = {**metadata, **self.adapter.apply(op["prepared"], image)}
            op["applied"][key] = (index, response)
            return response
        return metadata

    @staticmethod
    def error(exc):
        # Do not leak server credentials/URLs/tracebacks through exception strings.
        if isinstance(exc, KritaOperationError):
            return {"code": exc.code, "message": exc.message}
        return {"code": "AI_OPERATION_FAILED", "message": type(exc).__name__}

    @staticmethod
    def public(op):
        return {
            key: op[key]
            for key in (
                "operation_id",
                "kind",
                "state",
                "job_id",
                "progress",
                "error",
                "document_id",
            )
        } | {"result_count": len(op["results"])}

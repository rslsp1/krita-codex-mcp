"""Shared validation for the optional AI operation, including direct bridge callers."""

from __future__ import annotations

import math
from collections.abc import Mapping

ACTIONS = {
    "status": {"read"},
    "server": {"start", "stop", "connect", "disconnect", "refresh_models"},
    "catalog": {"styles", "checkpoints", "loras", "upscalers"},
    "generate": {"generate", "refine", "inpaint"},
    "jobs": {"list", "status", "cancel"},
    "result": {"read", "preview", "apply_new_layers"},
}
FIELDS = {
    "status": set(),
    "server": {"request_key"},
    "catalog": {"offset", "limit"},
    "generate": {
        "document_id",
        "request_key",
        "prompt",
        "negative_prompt",
        "style_id",
        "strength",
        "seed",
    },
    "jobs": {"operation_id"},
    "result": {"document_id", "operation_id", "result_index", "request_key", "max_dimension"},
}


def validate_ai(raw: Mapping) -> dict:
    command, action = raw.get("command"), raw.get("action")
    if not isinstance(command, str) or command not in ACTIONS:
        raise ValueError("Unknown AI command")
    if not isinstance(action, str) or action not in ACTIONS[command]:
        raise ValueError("Unknown AI action")
    if set(raw) - (FIELDS[command] | {"command", "action"}):
        raise ValueError("Unknown AI fields (replacement and arbitrary code are not supported)")
    result = dict(raw)
    for key in ("document_id", "request_key", "operation_id", "style_id"):
        if key in raw and (
            not isinstance(raw[key], str) or not raw[key].strip() or len(raw[key]) > 256
        ):
            raise ValueError(f"{key} must be a non-empty string of at most 256 characters")
    for key in ("prompt", "negative_prompt"):
        if key in raw and (not isinstance(raw[key], str) or len(raw[key]) > 8192):
            raise ValueError(f"{key} must be a string of at most 8192 characters")
    for key, low, high in (
        ("offset", 0, 100000),
        ("limit", 1, 100),
        ("result_index", 0, 63),
        ("seed", 0, 2**63 - 1),
        ("max_dimension", 64, 2048),
    ):
        if key in raw and (type(raw[key]) is not int or not low <= raw[key] <= high):
            raise ValueError(f"{key} must be an integer between {low} and {high}")
    if "strength" in raw:
        v = raw["strength"]
        if type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1:
            raise ValueError("strength must be finite and between zero and one")
    required = []
    if command in {"generate", "server"}:
        required.append("request_key")
    if command == "generate":
        required += ["document_id", "prompt", "style_id", "strength", "seed"]
    if command == "result" or (command == "jobs" and action != "list"):
        required.append("operation_id")
    if command == "result" and action == "apply_new_layers":
        required += ["document_id", "request_key"]
    if any(key not in raw for key in required):
        raise ValueError("Missing required AI fields: " + ", ".join(required))
    if command == "generate":
        if action == "generate" and raw["strength"] != 1:
            raise ValueError("generate requires strength=1; use refine for image-to-image")
        if action == "refine" and raw["strength"] >= 1:
            raise ValueError("refine requires strength below one")
    return result

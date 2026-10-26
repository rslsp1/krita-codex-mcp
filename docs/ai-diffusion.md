# AI Diffusion control (unreleased RSL fork)

This source-only extension adds six tools to the existing fourteen. It requires a matching
AI-enabled bridge installation and AI Diffusion **1.53.0** enabled inside Krita. Other plugin
versions report `AI_PLUGIN_UNSUPPORTED`; normal Krita tools continue to work without AI Diffusion.
It has no arbitrary Python or shell execution interface.

## Tools

| Tool | Scope |
|---|---|
| `krita_ai_status` | Read plugin availability/version, adapter capabilities and connection/server states; no document required |
| `krita_ai_server` | Schedule managed local server start/stop/connect/disconnect/model refresh |
| `krita_ai_catalog` | Paginated supported styles, checkpoint IDs, LoRA IDs and upscalers; no installation |
| `krita_ai_generate` | Submit one local generation, image refinement or selection-based inpainting operation |
| `krita_ai_jobs` | List bounded session history, poll an operation or cancel a queued owned job |
| `krita_ai_result` | Read result metadata, preview without changing Krita, or apply to new layers only |

All calls are dispatched through the existing authenticated loopback bridge to Krita's Qt main
thread. Long operations use AI Diffusion's event loop and return an `operation_id` before work
completes. Poll `krita_ai_jobs(action="status", operation_id=...)` for progress and structured
errors. An interrupted MCP connection does not mean the underlying job was cancelled.

Generation and server actions require `request_key`. Reuse the same key and identical arguments
on retries to avoid duplicate work. Keys persist for the Krita bridge session. Changing arguments
under an existing key fails. At most 32 operations are retained; history deliberately is not
automatically evicted because eviction would lose retry protection. Restart Krita to reset it.
Stored result pixels are limited to 128 MiB for the session. Operations from another session
return `AI_OPERATION_NOT_FOUND`.

## Example

1. Read `krita_ai_status()` and the explicit document ID using `krita_state()`.
2. Start/connect the local server with `krita_ai_server(action="start", request_key="start-1")`;
   poll its operation until it succeeds.
3. Choose a compatible style ID from `krita_ai_catalog(kind="styles")`.
4. Activate the intended document in Krita. For inpainting, make a nonempty partial selection.
5. Call `krita_ai_generate(document_id=..., mode="inpaint", prompt="red eyes",
   style_id=..., strength=0.45, seed=1234, request_key="eyes-1")`.
6. Poll until `succeeded`, then call `krita_ai_result(action="preview", operation_id=...)`.
7. Accept the result with `krita_ai_result(action="apply_new_layers", operation_id=...,
   document_id=..., request_key="accept-eyes-1")`, then inspect `krita_preview()`.

`generate` requires strength 1 and no selection/linked region. `refine` requires strength below 1
and no selection/linked region. `inpaint` requires the plugin to resolve the selection to an
inpaint/refine-region workflow. Full-canvas selections that the plugin treats as no mask are
rejected for inpainting. The first implementation rejects linked AI regions, edit mode and live
generation rather than guessing their semantics. Documents must be RGBA/U8 and within the
configured pixel limit. Prompt, negative prompt, style, strength and seed are captured
synchronously and the previous UI settings restored before submission.

## New layers are mandatory

MCP jobs deliberately use the plugin's prepared workflow and connected client but do **not** enter
`DocumentModel.jobs`. Therefore global `generation_finished_action=apply` cannot automatically
modify a source layer. MCP-owned results have their own bounded history and are applied only by
an explicit `apply_new_layers` call.

The adapter explicitly uses `ApplyBehavior.layer` and `ApplyRegionBehavior.none` with a copied
result context. It never calls the settings-dependent `apply_generated_result`. Existing source
layers and region layers are never overwritten or hidden by result application. No tool accepts
a replacement flag. The first implementation inserts a flattened raster result as a new top-level
layer, preserves the canvas size, and returns its UUID. Undo grouping is not promised until
validated in a live supported Krita build.

Apply requires the original document to remain open and active. Canvas geometry, color space
and projected input pixels are fingerprinted; changed inputs return `DOCUMENT_CHANGED` rather than
applying to an unrelated image. A successful application refreshes that operation's fingerprint
so another variant can be added as a new layer. Apply retries use their own `request_key`; identical
retries return the same created layer IDs. If application throws after a possible mutation, the
key remains reserved and retry reports `AI_APPLY_UNCERTAIN`: inspect the document before issuing
a new key. Do not treat that error as permission to submit another automatic apply.

## Deliberate limits

- Managed local servers only. No cloud billing, credential changes, remote URLs or downloads.
- No model installation, upgrade/uninstall or cache cleanup operations.
- No ControlNet setup, linked regions, model/LoRA overrides, custom workflows, animation, live
  mode or resizing/upscaling yet. Catalog visibility does not imply those controls are implemented.
- Only queued MCP-owned jobs can be cancelled by job ID. Running jobs report `AI_CANCEL_UNSAFE`;
  the adapter never invokes a server-wide interrupt or clears another document's queue.
- Session history is local to the bridge, not a durable queue. Preview responses are bounded PNGs;
  raw image bytes are not included in structured metadata.

## Development and rollout

Changes belong on a feature branch targeting `rsl-master`; follow [fork maintenance](rsl-fork.md).
The published upstream PyPI package and marketplace launcher do not contain this extension.
The wire operation `ai` is additive to protocol 1; old bridges reject it. The tool wrapper reports
`AI_BRIDGE_UPDATE_REQUIRED` on an unsupported-operation response. Install matching wheel and
bridge together through the existing installer; its hash manifest includes the new protocol
validation module and bridge adapter. Do not replace the bridge inside a running Krita session.

Before productive rollout, close Krita, install the built wheel/bridge, restart Krita and the
MCP host, then run the existing preflight and a disposable-document AI test: status/catalog,
start/connect, small generation, result preview, apply with original-layer pixel checks,
repeat-key check, changed-document rejection and Undo. Automated fake-adapter tests cover the
asynchronous operation lifecycle, ownership, input validation, UI-setting restoration and forced
new-layer path. They do not substitute for this GPU/Krita integration test.

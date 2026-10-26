// Exercise the packaged HTML against an MCP Apps host without external JS dependencies.
const { test } = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const vm = require("node:vm");
const html = readFileSync(join(__dirname, "../src/krita_codex_mcp/preview.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

function app() {
  const elements = new Map();
  for (const id of ["title", "status", "refresh", "canvas", "preview"]) {
    elements.set(id, { textContent: "", dataset: {}, hidden: true, disabled: true,
      listeners: {}, addEventListener(event, handler) { this.listeners[event] = handler; } });
  }
  const messages = [];
  const timers = new Map();
  const host = { postMessage(message) { messages.push(message); } };
  let onmessage;
  let timerId = 0;
  const context = {
    window: { parent: host, addEventListener(event, handler) { onmessage = handler; } },
    document: { getElementById: id => elements.get(id), documentElement: { style: {} } },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout(id) { timers.delete(id); },
  };
  vm.runInNewContext(script, context);
  const receive = data => onmessage({ source: host, data: { jsonrpc: "2.0", ...data } });
  const tick = () => new Promise(resolve => setImmediate(resolve));
  return {
    elements, messages, receive, timers,
    async init(serverTools = {}) {
      assert.equal(messages[0].method, "ui/initialize");
      receive({ id: messages[0].id, result: {
        protocolVersion: "2026-01-26", hostCapabilities: { serverTools }, hostContext: {}
      } });
      await tick();
      assert.equal(messages[1].method, "ui/notifications/initialized");
    },
    sendFromStranger(data) { onmessage({ source: {}, data: { jsonrpc: "2.0", ...data } }); },
  };
}

const result = (name = "Document") => ({
  content: [{ type: "image", mimeType: "image/png", data: "aGVsbG8=" }],
  structuredContent: { document_name: name, width: 64, height: 64 }
});

test("initial image and safe document name display", async () => {
  const a = app();
  await a.init();
  a.receive({ method: "ui/notifications/tool-result", params: result("<img onerror=alert(1)>") });
  assert.equal(a.elements.get("preview").src, "data:image/png;base64,aGVsbG8=");
  assert.equal(a.elements.get("title").textContent, "<img onerror=alert(1)>");
  assert.equal(a.elements.get("canvas").hidden, false);
});

test("refresh preserves crop and document targeting and serializes requests", async () => {
  const a = app();
  await a.init();
  const args = { document_id: "doc-1", x: 1, y: 2, width: 3, height: 4, scale: 2 };
  a.receive({ method: "ui/notifications/tool-input", params: { arguments: { ...args, output_path: "bad" } } });
  const button = a.elements.get("refresh");
  const refreshing = button.listeners.click();
  assert.equal(button.disabled, true);
  const request = a.messages.at(-1);
  assert.equal(request.method, "tools/call");
  assert.equal(request.params.name, "krita_preview");
  assert.deepEqual(JSON.parse(JSON.stringify(request.params.arguments)), args);
  await button.listeners.click();
  assert.equal(a.messages.at(-1), request);
  a.receive({ id: request.id, result: result() });
  await refreshing;
  assert.equal(button.disabled, false);
  assert.equal(a.elements.get("canvas").hidden, false);
});

test("unspecified document stays active instead of pinning returned document_id", async () => {
  const a = app();
  await a.init();
  a.receive({ method: "ui/notifications/tool-result", params: {
    ...result(), structuredContent: { document_id: "old-doc" }
  } });
  const refreshing = a.elements.get("refresh").listeners.click();
  const request = a.messages.at(-1);
  assert.equal(request.params.arguments.document_id, undefined);
  a.receive({ id: request.id, result: result("New document") });
  await refreshing;
  assert.equal(a.elements.get("title").textContent, "New document");
});

test("bridge errors and missing image clear stale preview", async () => {
  const a = app();
  await a.init();
  a.receive({ method: "ui/notifications/tool-result", params: result() });
  a.receive({ method: "ui/notifications/tool-result", params: {
    isError: true, content: [{ type: "text", text: "NO_ACTIVE_DOCUMENT" }]
  } });
  assert.equal(a.elements.get("canvas").hidden, true);
  assert.equal(a.elements.get("status").textContent, "NO_ACTIVE_DOCUMENT");
  a.receive({ method: "ui/notifications/tool-result", params: { content: [] } });
  assert.match(a.elements.get("status").textContent, /no PNG/);
});

test("RPC errors and timeouts allow retry", async () => {
  const a = app();
  await a.init();
  const button = a.elements.get("refresh");
  let refreshing = button.listeners.click();
  a.receive({ id: a.messages.at(-1).id, error: { message: "Disconnected" } });
  await refreshing;
  assert.equal(button.disabled, false);
  assert.equal(a.elements.get("status").textContent, "Disconnected");
  refreshing = button.listeners.click();
  [...a.timers.values()][0]();
  await refreshing;
  assert.equal(button.disabled, false);
  assert.match(a.elements.get("status").textContent, /timed out/);
});

test("stranger messages are ignored and hosts without serverTools remain read only", async () => {
  const a = app();
  await a.init(null);
  assert.equal(a.elements.get("refresh").disabled, true);
  const count = a.messages.length;
  await a.elements.get("refresh").listeners.click();
  assert.equal(a.messages.length, count);
  a.sendFromStranger({ method: "ui/notifications/tool-result", params: result("Stranger") });
  assert.equal(a.elements.get("canvas").hidden, true);
  a.receive({ method: "ui/notifications/tool-result", params: result("Host") });
  assert.equal(a.elements.get("title").textContent, "Host");
});

test("teardown cancels pending calls and disables refresh", async () => {
  const a = app();
  await a.init();
  const refreshing = a.elements.get("refresh").listeners.click();
  a.receive({ method: "ui/resource-teardown", id: "close", params: {} });
  await refreshing;
  assert.equal(a.elements.get("refresh").disabled, true);
  assert.equal(a.timers.size, 0);
  assert.equal(a.messages.at(-1).id, "close");
});

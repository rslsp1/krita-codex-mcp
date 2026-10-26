# Document preview in the RSL fork

## Three display paths

1. **Standard MCP image:** `krita_preview` returns an explicit `CallToolResult` containing the
   PNG `ImageContent` first. Text and `structuredContent` contain metadata only, so the image is
   not also serialized into a second base64 payload. `krita_asset` uses the same result format.
2. **MCP Apps view:** supporting hosts discover `ui://krita-codex-mcp/document-preview.html` from
   `krita_preview`'s `_meta.ui.resourceUri`. The self-contained HTML shows the document name,
   preview size, update time and a Refresh button. It uses the stable MCP Apps `2026-01-26`
   handshake and calls only the read-only `krita_preview` tool through the host. It has no CDN,
   external network requests, polling, bridge credentials or direct localhost access.
   In hosts supporting OpenAI conversation-panel entrypoints, the thread's side-panel app menu
   also lists **Krita document preview**. Open that entry to use a connected panel; opening the
   HTML file directly in a browser does not establish an MCP connection. Panel launches read
   the initial image automatically; Refresh then reads it again on demand.
3. **PNG file:** `krita_preview_file` writes the same projection beneath a configured allowed
   root and returns its absolute `preview_path`. An agent can use a client's supported local-file
   image syntax to display it. This is useful when the client does not render MCP tool images.

MCP Apps support is optional. The image-first result alone does not guarantee mobile display.
The app can display the initial result without `serverTools`; Refresh is enabled only when the
host advertises tool-call support. Refresh preserves explicit document IDs, crops and size bounds.
Without a document ID it follows the active document, including manual document switches.
Errors hide stale images and allow a retry. Refresh neither edits nor saves a document.

The file tool requires an explicit `.png` destination and an existing parent directory, honors
allowed roots and defaults to refusing overwrite. `overwrite=true` replaces the file only after
a valid preview has been received and staged successfully. Preview size/byte limits apply to all
paths. Preview-file creation is a filesystem write and is annotated accordingly.

## File display example

Call `krita_preview_file` with `output_path` such as `C:\Krita Work\preview.png` on Windows or
`/home/user/krita-work/preview.png` on Linux, **only if that directory is already an allowed root**.
Use the returned path, rather than assuming that the example path exists. For clients with local
Markdown image support, the final response can contain:

```markdown
![Krita document preview](/absolute/path/from/preview_path.png)
```

The returned path belongs to the MCP server's filesystem. A remote client/agent must be able to
access that file through its supported attachment handling, file transfer or path mapping. A
Windows path does not automatically become a Linux path or a public download URL. Never paste
base64 into the final answer, invent a link, or claim successful remote display without evidence.

## Run the fork from source

These additions are unreleased. The existing marketplace manifest and generated installer
configuration still select the original, version-pinned PyPI package. Merely cloning the fork
does not activate its server. The server and installed Krita bridge must report the same exact
distribution version (`0.2.2` for this source version). The preview feature adds no bridge
operation, but an older bridge such as `0.2.0` still requires an update: the server deliberately
rejects it with `BRIDGE_VERSION_MISMATCH` even when the protocol version matches.

Before updating an older installation, save open documents and close Krita. Run the fork's
`krita-codex-install update` using the same source environment or built wheel as the new server.
This preserves the existing authenticated configuration, token, port and allowed roots. Run
`krita-codex-install check --offline`, reopen Krita and complete its live check before switching
the client launcher. Do not replace bridge files while Krita is running or bypass version checks.

For local use after the PR is merged:

```bash
git clone --branch rsl-master https://github.com/rslsp1/krita-codex-mcp.git
cd krita-codex-mcp
uv sync --locked --all-groups --python 3.12
uv run --locked --no-dev krita-codex-mcp
```

For a configured MCP launcher, use `uv` as the command and arguments
`["--directory", "<absolute checkout path>", "run", "--locked", "--no-dev", "krita-codex-mcp"]`.
Use the same platform, configuration path and environment as the working Krita server. Changing
an existing client configuration is a separate installation step; do not run two Krita MCP entries
at once. Restart the client after switching launchers. Before merging, test with the feature
branch checkout instead of `rsl-master`.

## Verification scope

Python integration tests exercise tool/resource discovery, image results and preview-file
operations through MCP sessions, using a fake bridge. Dependency-free JavaScript tests exercise
the packaged UI handshake, rendering, targeting, refresh, errors, timeouts and teardown. CI also
checks the built wheel and its STDIO tool surface. These checks do not replace a live test with
Krita and the intended desktop/mobile host after installing the fork.

Protocol reference: [MCP Apps stable specification](https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx).

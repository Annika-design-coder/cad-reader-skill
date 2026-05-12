# cad-reader

`cad-reader` is a portable Agent skill for evidence-backed CAD drawing understanding. It reads DWG, DXF, and drawing-style PDF files through a configured remote BricsCAD reader service, then returns structured evidence and optional preview images for an Agent to explain to users.

It is not a pure prompt-only skill. A host Agent must provide a backend adapter that can save uploaded files, run the Python wrapper, parse JSON stdout, and expose generated PNG files to the user.

## What It Does

- Answer natural-language questions about DWG/DXF/drawing-PDF content.
- Inspect drawing metadata, layouts, layers, text, dimensions, blocks, and proxy objects.
- Render whole drawings or CAD coordinate regions through remote BricsCAD.
- Query text, dimensions, layers, block names, attributes, and proxy hints.
- Provide basic count, length, area, and point-distance evidence.
- Diagnose Tianzheng/T20/proxy-object readability limitations.

It is not intended as the primary workflow for formal BOQ, pricing, contractual quantity takeoff, code certification, or professional design-liability review.

## Package Layout

```text
SKILL.md                         Agent-facing workflow and behavioral rules
agent_tools.json                 Machine-readable tool manifest
scripts/cad_reader_tool.py       Stable Python CLI wrapper
scripts/remote_bricscad_server.py Private remote BricsCAD service reference
scripts/test_cad_reader_tool.py  Local smoke tests
references/agent_integration.md  Detailed adapter guide
references/remote_bricscad_deploy.md Remote service deployment notes
agents/openai.yaml               OpenAI/Codex UI metadata only
```

## Requirements

- Python 3.10 or newer for the wrapper.
- A host backend that can run subprocesses or wrap the CLI behind an internal HTTP tool.
- A configured remote BricsCAD reader endpoint.
- A host UI or attachment system that can display PNG files returned by the tool.

The wrapper currently uses only the Python standard library. `requirements.txt` is kept as a stable extension point.

## Configuration

Copy `.env.example` to `.env.local` in the skill directory and set your own CAD reader service:

```env
CADLIST_DWG_READER_PRIORITY=remote-bricscad
CADLIST_REMOTE_BRICSCAD_URL=https://your-private-cad-reader/cad
CADLIST_REMOTE_BRICSCAD_TOKEN=your-token
CADLIST_REMOTE_BRICSCAD_TIMEOUT=900
```

The public package does not include a shared managed CAD service. If `CADLIST_REMOTE_BRICSCAD_URL` is missing, the wrapper fails closed. Do not silently fall back to local CAD software, local conversion, or third-party public endpoints.

## Smoke Test

```powershell
python scripts/test_cad_reader_tool.py --skip-remote
python scripts/cad_reader_tool.py --work-root . health
```

Without a remote endpoint, `health` should return structured JSON with `ok: false` and `missing CADLIST_REMOTE_BRICSCAD_URL`.

## Minimal Tool Call

```powershell
python scripts/cad_reader_tool.py --work-root . ask --file "C:\uploads\drawing.dwg" --question "这张施工图有没有可能影响后续施工的问题？"
```

The host adapter must pass a host-created, allowlisted absolute file path. Do not pass arbitrary user-written paths unless the platform has verified and authorized them.

## Host Adapter Responsibilities

- Save uploads into an allowlisted workspace.
- Reject symlink escapes, parent-directory escapes, unsupported extensions, and unapproved network paths.
- Run the wrapper with argv arrays rather than shell-built command strings.
- Parse stdout as JSON and keep stderr in operator logs only.
- Copy returned image paths such as `preview_image` into a user-visible attachment, signed URL, or platform file object.
- Caption displayed images with a reason, suspected issue, and evidence strength.
- Keep full `extract.json`, raw logs, local paths, tokens, and stack traces out of ordinary user messages and model context.

See `references/agent_integration.md` for the full integration contract.

## Remote BricsCAD Service

`scripts/remote_bricscad_server.py` is a private deployment reference, not a public hosted service. For non-loopback listening, configure a token and run behind HTTPS/reverse proxy, upload limits, concurrency limits, logging policy, and retention controls.

## License

Apache-2.0. See `LICENSE`.

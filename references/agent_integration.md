# Agent Integration

This reference is for integrating `cad-reader` into non-Codex Agent runtimes such as LangChain, Dify, AutoGen, custom backend agents, or workflow engines.

The skill is a local Python wrapper around a managed CAD reader service. The end user uploads a drawing and asks natural-language questions; the Agent saves the file, calls the wrapper, parses JSON stdout, and displays returned PNG evidence when useful.

## Contents

- Data path and privacy
- Runtime requirements
- Hosting models and adapter responsibilities
- Primary `cad.ask` tool
- Lower-level tools
- Adapter schema
- JSON and error handling
- Output files
- Timeouts and large files
- Managed endpoint controls
- User-facing response policy
- Smoke tests

## Data Path And Privacy

Default behavior sends DWG/DXF/drawing-PDF content to the configured CAD reader service when a CAD command needs remote reading, rendering, extraction, or diagnostics.

Data that may be processed by the CAD reader service includes:

```text
original drawing file
converted/intermediate drawing data
rendered preview images
extracted text, dimensions, layers, blocks, and object metadata
service errors and operational metadata
```

Before production use, the operator must decide whether this is acceptable for the target users and projects.

For confidential or regulated drawings:

```text
Option A: deploy a private CAD reader service and set CADLIST_REMOTE_BRICSCAD_URL
Option B: use an approved organization-hosted endpoint
Option C: do not enable this skill for those users
```

The public skill package does not include a shared managed CAD service. If no reachable `CADLIST_REMOTE_BRICSCAD_URL` is configured, the integration should fail closed. Do not silently fall back to public third-party services or local CAD conversion.

If a deployment uses fallback endpoints, treat every fallback as part of the data-processing boundary. Regulated deployments should disable cross-service fallback unless it has been approved.

Before public release, use a formal domain, HTTPS, managed secrets, upload limits, logging policy, data-retention rules, and monitoring.

Do not expose BricsCAD, internal URLs, tokens, environment variables, stack traces, or script names to ordinary end users.

Do not send full `extract.json`, raw logs, or large CAD text dumps into model context. Select only the minimum matches, preview paths, and evidence snippets needed for the answer. Include LLM traces, prompt replay systems, analytics, and evaluation logs in the privacy boundary.

## Runtime Requirements

Minimum requirements:

- Python 3.10 or newer, preferably in a virtual environment.
- Ability to run a subprocess with a 900 second timeout for large drawings.
- Ability to save uploaded files to local disk.
- Ability to read JSON from stdout.
- Ability to display local PNG files returned by the tool.
- Network access from the Agent runtime to the configured CAD reader endpoint.

Install dependencies from the skill directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The wrapper automatically loads environment files from the skill directory when present:

```text
<skill_dir>\.env
<skill_dir>\.env.local
```

Use `.env.local` for private endpoints, tokens, and timeout overrides. Do not commit secrets.

Recommended command style:

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" ask --file "<absolute drawing path>" --question "<user question>"
```

Use absolute paths for both `<skill_dir>` and uploaded drawing files. This avoids cwd differences between Agent platforms.

## Hosting Models And Adapter Responsibilities

This skill is not a pure LLM function. It requires a controlled backend that can save files, run Python, call the CAD reader service, and expose generated images to the user.

Supported patterns:

- backend Agent with local subprocess access;
- workflow server that wraps the Python command as an internal tool;
- SaaS Agent UI plus a private HTTP adapter owned by the integrator.

Unsupported without an adapter:

- pure hosted Agent UI with no filesystem;
- tool runtimes that cannot run subprocesses;
- frontends that cannot turn backend-local PNG paths into user-visible attachments.

Adapter responsibilities:

- Save uploads into an allowlisted upload/workspace directory. Do not accept a user-written arbitrary `file` path.
- Canonicalize paths and reject symlink escapes, parent-directory escapes, unexpected extensions, and unapproved network/UNC paths.
- Rename or map uploads to internal IDs when filenames reveal sensitive project information.
- Run the wrapper with fixed command templates; never build shell strings from raw user input.
- Do not add fallback paths that invoke local desktop CAD software, local CAD readers, ODA/Teigha viewers, LibreCAD/QCAD, or ad-hoc parser libraries as replacement DWG/DXF readers.
- Do not write adapter-side scripts that locally convert DWG/DXF/PDF into DXF, SVG, PNG, image tiles, or another surrogate format for CAD understanding. Rendering and extraction must come from the `cad-reader` wrapper and the configured remote BricsCAD service.
- Validate returned PNG files exist, have expected MIME/extension, and are within size limits.
- Convert backend-local PNG paths to platform-visible attachments, short-lived signed URLs, base64 payloads, or the platform's file object. Do not show local paths to ordinary users.
- Isolate `output/cad_reader` cache by tenant/user/session in multi-tenant deployments, or enforce strict access controls.
- Minimize model context: pass only relevant matches, risks, and image references, not complete extraction dumps.
- Keep stderr and raw logs in secure operator logs only; do not pass them to users or the LLM unless redacted.
- Prefer asynchronous jobs or polling for large drawings. Support cancellation, queue limits, per-user/per-tenant concurrency limits, and file-size limits.

## Primary Tool: cad.ask

Use `cad.ask` for most user-facing drawing questions. It is the preferred conversational entrypoint.

Command:

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" ask --file "<drawing>" --question "<question>"
```

Required arguments:

```text
--file      absolute path to DWG/DXF/drawing-PDF
--question  user's natural-language question
```

Useful optional arguments:

```text
--force       ignore cache and re-read the drawing
--limit N     max matched objects returned; default 20
--no-render   return evidence without generating preview images
```

Success JSON shape:

```json
{
  "ok": true,
  "tool": "cad.ask",
  "question": "帮我看看这张施工图有没有明显不对劲的地方",
  "answer": "draft answer text",
  "preview_image": "E:\\cad-reader-skill\\output\\cad_reader\\...\\ask_focus.png",
  "focus_images": [
    {
      "kind": "overview",
      "preview_image": "E:\\cad-reader-skill\\output\\cad_reader\\...\\ask_overview.png",
      "bbox": [0, 0, 100, 100],
      "render_mode": "remote-bricscad-screenshot"
    }
  ],
  "match_count": 3,
  "matches": [],
  "focus_id": "ask_...",
  "previous_bbox": [0, 0, 100, 100],
  "evidence_id": "a1b2c3d4e5f6...",
  "evidence_strength": "moderate",
  "render_warnings": [],
  "risks": [],
  "fallback": {},
  "conversion_request": "",
  "evidence": "E:\\cad-reader-skill\\output\\cad_reader\\...\\ask_evidence.json"
}
```

Failure JSON shape:

```json
{
  "ok": false,
  "tool": "cad.ask",
  "error": "remote CAD service returned no structured payload",
  "status": 0,
  "file": {
    "path": "E:\\uploads\\drawing.dwg",
    "extension": ".dwg",
    "supported": true
  }
}
```

The Agent should not blindly copy `answer`. Use it as a draft, inspect `preview_image`, `focus_images`, `fallback`, `risks`, and `evidence`, then write a natural final response.

For broad first-pass risk questions such as “帮我看看这张施工图有没有明显不对劲”:

```text
1. Run cad.ask as the draft pass.
2. Run cad.inspect or cad.diagnose to check readability, layouts, units, proxy/Tianzheng risks, missing text/dimensions, and extraction limits.
3. Display at least one overview image if available.
4. For each concrete suspected issue, run cad.region --render before presenting it as a visible observation.
5. If the issue involves distance, area, clear width, or count, use cad.measure or CAD object data before confident wording.
6. If no issue is found, scope the conclusion: “在已成功读取和渲染的范围内暂未发现明显异常”.
```

Internal broad-scan checklist:

```text
title block / drawing name / version / layout
scale / units / extents / huge-coordinate or blank-layout anomalies
axes / grids / dimensions / elevation labels
room names / component marks / door-window IDs
overlap / truncation / missing or inconsistent text and dimensions
missing previews / missing CAD object data / Tianzheng-T20 proxy objects
```

Do not output the checklist as a table unless the user explicitly asks for one.

## Lower-Level Tools

Use lower-level tools when the Agent needs multi-step reasoning or stronger evidence.

### cad.health

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" health
```

Use for deployment checks and debugging, not ordinary user questions.

### cad.render

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" render --file "<drawing>"
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" render --file "<drawing>" --bbox XMIN YMIN XMAX YMAX
```

Use for whole-drawing or local-region rendering. A successful render must come from the remote BricsCAD service and returns `render_mode: "remote-bricscad-screenshot"`.

If remote rendering fails, the wrapper returns a structured failure such as `render_mode: "remote-bricscad-failed"` and does not generate a local substitute image. The Agent may continue with structured CAD evidence only if it clearly labels the result as weak/degraded.

### cad.inspect

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" inspect --file "<drawing>" --preview
```

Returns drawing summary:

```text
file type, extents, layouts, layers, entity counts, text samples, dimension samples, proxy summaries, risks, optional preview image
```

### cad.query

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" query --file "<drawing>" --pattern "<text or regex>" --regex --limit 50
```

Use for text, dimensions, block names, layers, attributes, object names, and proxy type hints.

Important fields:

```text
matches
focus_bbox
evidence
```

### cad.region

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" region --file "<drawing>" --bbox XMIN YMIN XMAX YMAX --render
```

`--bbox` order is:

```text
XMIN YMIN XMAX YMAX
```

Use this to produce local preview images and inspect objects inside the region.

### cad.measure

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" measure --file "<drawing>" --bbox XMIN YMIN XMAX YMAX --types INSERT
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" measure --file "<drawing>" --points X1 Y1 X2 Y2
```

Use for evidence-level counts and basic CAD distances/areas. Do not present this as a formal quantity takeoff unless another workflow validates it.

### cad.diagnose

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" diagnose --file "<drawing>"
```

Use for first-pass drawing readability risks, proxy/Tianzheng signals, missing dimensions, missing text, and conversion request generation.

## Adapter Schema

`agent_tools.json` is a compact discovery list. Tool adapters for production Agents should implement the fuller behavior below.

Common coordinate rules:

```text
bbox order: [xmin, ymin, xmax, ymax]
values are CAD drawing coordinates in the drawing unit
negative values are allowed when present in the drawing
require xmin < xmax and ymin < ymax
```

Common follow-up state:

```text
focus_id       stable handle for the current rendered or queried focus
previous_bbox  CAD bbox to reuse when the user says “这里/刚才那个区域/放大一点”
evidence_id    parent evidence id for audit and follow-up verification
```

Some model providers do not accept dots in native tool names. `agent_tools.json` includes `provider_safe_name` and `aliases` such as `cad_ask`, `cad_inspect`, `cad_render`, `cad_query`, `cad_region`, `cad_measure`, `cad_diagnose`, and `cad_health`; adapters should map those names back to the canonical commands.

The canonical machine-readable schema is now in `agent_tools.json`. The following minimal standard schema is retained as explanatory documentation:

```json
{
  "cad.ask": {
    "parameters": {
      "type": "object",
      "required": ["file", "question"],
      "additionalProperties": false,
      "properties": {
        "file": {"type": "string", "description": "host-created absolute DWG/DXF/drawing-PDF path inside the upload allowlist"},
        "question": {"type": "string", "minLength": 1},
        "force": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "no_render": {"type": "boolean"},
        "focus_id": {"type": "string"},
        "previous_bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "evidence_id": {"type": "string"}
      }
    },
    "timeout_seconds": 900
  },
  "cad.inspect": {
    "parameters": {
      "type": "object",
      "required": ["file"],
      "additionalProperties": false,
      "properties": {
        "file": {"type": "string"},
        "preview": {"type": "boolean"},
        "force": {"type": "boolean"}
      }
    },
    "timeout_seconds": 900
  },
  "cad.render": {
    "parameters": {
      "type": "object",
      "required": ["file"],
      "additionalProperties": false,
      "properties": {
        "file": {"type": "string"},
        "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "focus_id": {"type": "string"},
        "previous_bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "evidence_id": {"type": "string"}
      }
    },
    "timeout_seconds": 900
  },
  "cad.query": {
    "parameters": {
      "type": "object",
      "required": ["file", "pattern"],
      "additionalProperties": false,
      "properties": {
        "file": {"type": "string"},
        "pattern": {"type": "string", "minLength": 1},
        "regex": {"type": "boolean"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500}
      }
    },
    "timeout_seconds": 900
  },
  "cad.region": {
    "parameters": {
      "type": "object",
      "required": ["file"],
      "additionalProperties": false,
      "properties": {
        "file": {"type": "string"},
        "bbox": {
          "type": "array",
          "items": {"type": "number"},
          "minItems": 4,
          "maxItems": 4,
          "description": "[xmin, ymin, xmax, ymax]; enforce xmin < xmax and ymin < ymax"
        },
        "render": {"type": "boolean"},
        "focus_id": {"type": "string"},
        "previous_bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "evidence_id": {"type": "string"}
      }
    },
    "timeout_seconds": 900
  },
  "cad.measure": {
    "parameters": {
      "type": "object",
      "required": ["file"],
      "additionalProperties": false,
      "properties": {
        "file": {"type": "string"},
        "bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "points": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4, "description": "[x1, y1, x2, y2]"},
        "types": {"type": "string"},
        "layers": {"type": "string"},
        "focus_id": {"type": "string"},
        "previous_bbox": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
        "evidence_id": {"type": "string"}
      }
    },
    "timeout_seconds": 900
  },
  "cad.diagnose": {
    "parameters": {
      "type": "object",
      "required": ["file"],
      "additionalProperties": false,
      "properties": {
        "file": {"type": "string"},
        "force": {"type": "boolean"}
      }
    },
    "timeout_seconds": 900
  },
  "cad.health": {
    "parameters": {"type": "object", "additionalProperties": false, "properties": {}},
    "timeout_seconds": 60
  }
}
```

## JSON And Error Handling

Always parse stdout as JSON first.

Expected subprocess behavior:

```text
0  success, usually JSON with ok=true
1  health/test failure, may return JSON
2  user/file/service error, usually JSON with ok=false
other  invocation/runtime failure, may not return JSON
```

Argument parser errors, Python dependency failures, and process-level crashes may print non-JSON text. Treat non-JSON stdout/stderr as a tool invocation failure, hide raw logs from the end user, and respond with a recovery message.

Result priority:

```text
1. If stdout is valid JSON, use JSON ok/error as the business result.
2. Use the process exit code as a runner/transport signal, not as the only success criterion.
3. If exit code is non-zero but JSON is valid, preserve the structured JSON for Agent reasoning.
4. If exit code is zero but JSON has ok=false, treat it as a tool/business failure.
5. If stdout is empty or non-JSON, construct an adapter error such as {"ok": false, "error_type": "tool_invocation_failed"}.
6. Store stderr only in secure logs after redaction; do not put raw stderr into model context or user messages.
```

Typical output fields by tool:

```text
cad.ask      answer, preview_image, focus_images, render_warnings, matches, fallback, focus_id, previous_bbox, evidence_id, evidence_strength, evidence
cad.inspect  data/summary, preview_image/full_preview, risks, fallback
cad.render   preview_image, bbox, render_mode, render_error, focus_id, evidence_id, evidence_strength, evidence
cad.query    matches, focus_bbox, focus_id, evidence_id, evidence
cad.region   items, preview_image, render_mode, render_error, bbox, focus_id, evidence_id, evidence_strength, evidence
cad.measure  data/result, sample_items, focus_id, evidence_id, evidence
cad.diagnose answer, risks, fallback, conversion_request, focus_id, focus_bbox, evidence_id, evidence_strength, evidence
cad.health   status/service checks
```

Field caveats:

- `preview_image` may be empty when rendering is disabled or unavailable.
- `render_mode` is `remote-bricscad-screenshot` only when visual evidence came from the remote BricsCAD renderer. `remote-bricscad-failed` means no local substitute render was created.
- `focus_bbox` may be null when no reliable match exists.
- `focus_id`, `previous_bbox`, and `evidence_id` should be saved by the adapter and passed into follow-up calls.
- `measure --points` may return a numeric distance without local image evidence unless the Agent separately renders the region.
- Evidence paths are local implementation details; cite them only for audit/debug needs.

## Output Files

The wrapper writes cache and evidence under:

```text
<skill_dir>\output\cad_reader\<drawing_id>\
```

Typical files:

```text
extract.json
summary.json
ask_evidence.json
*.png
天正T20代理对象转换要求.md
```

Agents may display PNG files directly only when the host UI can access backend-local paths. Most non-Codex integrations must copy PNGs into an attachment system, object storage, signed URL, base64 payload, or platform file object.

Evidence JSON is mainly for audit, follow-up reasoning, and debugging.

Do not expose full local evidence paths or original upload paths to ordinary users unless they ask for audit details or the host application needs a file reference.

The integration may periodically clean old output folders, but should not delete evidence during an active conversation.

## Timeouts And Large Files

Recommended subprocess timeout:

```text
health: 30-90 seconds
inspect/diagnose first pass: 180 seconds
query/measure: 480 seconds
render/region: 720-900 seconds
ask: 900-1200 seconds when preview rendering is enabled
```

Also set service timeouts in the environment when large drawings are expected:

```powershell
$env:CADLIST_REMOTE_BRICSCAD_TIMEOUT="900"
$env:CADLIST_TANGENT_WORKER_TIMEOUT="900"
```

For persistent configuration, put those values in `<skill_dir>\.env.local`.

Large DWG files can take minutes. Do not start many CAD reads concurrently against the same hosted service unless the service has been sized for that load. In a broad project scan, keep `inspect`/`diagnose` bounded and skip timed-out drawings for separate retry instead of blocking the whole chat.

For web products, avoid holding a frontend request open for 900 seconds. Use a background job, progress state, polling, or resumable workflow. On timeout, terminate the subprocess when possible and tell the user the file may be large or the service may be busy; suggest specifying a layout/region, retrying later, or providing a same-version PDF.

## Remote Endpoint Controls

The public package is unconfigured by default:

```text
CADLIST_REMOTE_BRICSCAD_URL=
CADLIST_REMOTE_BRICSCAD_TOKEN=
```

Configure your own endpoint:

```powershell
$env:CADLIST_REMOTE_BRICSCAD_URL="https://your-private-cad-reader/cad"
$env:CADLIST_REMOTE_BRICSCAD_TOKEN="<token>"
```

You can explicitly disable any built-in/managed endpoint behavior in derived private forks:

```powershell
$env:CADLIST_MANAGED_REMOTE="0"
```

During testing, a private fork may add fallback endpoints if the primary endpoint is unavailable. This is an operator concern; do not expose it to ordinary users. In regulated deployments, disable or approve fallback endpoints as part of the same data boundary.

## User-Facing Response Policy

The Agent should respond in this shape:

```text
结论：...
我看的区域：展示 preview_image 或 focus_images
依据：文字/尺寸/图层/对象/局部图
证据强度：可以确认 / 可以初步判断 / 只能从图面观察
不确定性：代理对象、单位、缺失文字、弱证据
需要时的下一步：转换版 DWG、PDF、指定区域、私有部署
```

Use evidence-strength language:

```text
strong: 可以确认……
moderate: 可以初步判断……
weak: 只能从局部预览图观察到……不能当作 CAD 对象数据确认。
```

For broad risk answers:

```text
Show at least one overview preview when available.
Show a local rendered image for each specific suspicious location you report.
If no preview is available, say whether the conclusion is based only on structured CAD data or cannot be reliably judged visually.
Never say “图纸没有问题”; say “在已读取/已渲染范围内暂未发现明显异常”.
```

Map internal failures to friendly wording:

```text
Service unavailable: CAD 读图服务这次没有返回可用结果，可以稍后重试或先用 PDF 做可视化检查。
Tianzheng/T20 blocked: 图里有部分天正/T20 特殊对象没有展开，未展开内容不能作为尺寸或数量依据。
Preview only: 目前只有图面预览，没有对应 CAD 对象数据，所以只能做观察性判断。
Unsupported file: 这个文件类型目前不能作为 CAD 图纸读取，请提供 DWG、DXF 或图纸 PDF。
File too large or timeout: 这张图读取时间过长，可以指定某个布局/区域，或提供同版 PDF 先做局部检查。
Private endpoint missing: 当前策略不允许上传到托管读图服务，也没有配置私有读图服务，所以我不能读取这张图。
Authentication/configuration: CAD 读图服务配置需要管理员处理，当前不能可靠读取图纸。
JSON/tool invocation failure: 读图工具这次没有返回可解析结果，我不能把它当作有效证据。
Remote BricsCAD unavailable and no valid input alternative: 当前不能可靠读取 DWG/DXF；我不能在本地自行转换图纸格式来替代 CAD 读图。可以恢复读图服务，或由出图方提供同版 PDF/已转换为普通 AutoCAD 对象的 DWG。
```

Avoid raw logs, stack traces, URLs, tokens, environment variables, script names, and server implementation details.

## Smoke Tests

Local test without remote CAD:

```powershell
python "<skill_dir>\scripts\test_cad_reader_tool.py" --skip-remote
```

Full test with a sample drawing:

```powershell
python "<skill_dir>\scripts\test_cad_reader_tool.py" --file "<sample.dwg>" --bbox XMIN YMIN XMAX YMAX
```

Direct health check:

```powershell
python "<skill_dir>\scripts\cad_reader_tool.py" --work-root "<skill_dir>" health
```

Adapter acceptance checks:

```text
parse stdout JSON for ok=true and ok=false
convert non-JSON output to a safe adapter error
enforce upload path allowlist and reject arbitrary local paths
verify timeout and cancellation behavior
turn returned PNG paths into user-visible attachments
confirm missing CADLIST_REMOTE_BRICSCAD_URL fails closed
confirm remote-service failure does not fall back to local CAD software or local format-conversion scripts
verify raw stderr/logs are not sent to the LLM or user
check multi-tenant cache/output isolation
```


from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "agent_tools.json"
SERVER = ROOT / "scripts" / "remote_bricscad_server.py"


def check(condition: bool, message: str) -> dict[str, object]:
    return {"ok": bool(condition), "message": message}


def main() -> int:
    results: list[dict[str, object]] = []
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    results.append(check(manifest.get("schema_version") == "cad-reader.agent-tools.v1", "manifest has schema version"))
    results.append(check(manifest.get("environment", {}).get("fail_closed_without_required") is True, "manifest declares fail-closed env policy"))
    tools = manifest.get("tools") or []
    results.append(check(len(tools) >= 8, "manifest declares all CAD tools"))
    for tool in tools:
        name = str(tool.get("name") or "")
        safe = str(tool.get("provider_safe_name") or "")
        params = tool.get("parameters_schema") or {}
        results.append(check(bool(name and safe and re.match(r"^[A-Za-z0-9_]+$", safe)), f"{name} has provider-safe name"))
        results.append(check(params.get("type") == "object" and params.get("additionalProperties") is False, f"{name} has strict parameter schema"))
        results.append(check(isinstance(tool.get("timeout_seconds"), int), f"{name} has timeout"))
    display = manifest.get("adapter_display_policy", {})
    results.append(check("required_host_context" in display, "image display context is host metadata"))
    bad_return = json.dumps(tools, ensure_ascii=False)
    results.append(check("display_reason" not in bad_return and "suspected_issue" not in bad_return, "display caption fields are not declared as CLI returns"))

    server_text = SERVER.read_text(encoding="utf-8")
    results.append(check('BRICSCAD_SERVER_HOST", "127.0.0.1"' in server_text, "remote server defaults to loopback"))
    results.append(check("BRICSCAD_SERVER_TOKEN is required when listening on a non-loopback host" in server_text, "remote server requires token on non-loopback"))
    results.append(check("max_upload_bytes" in server_text and "max_entity_limit" in server_text and "BoundedSemaphore" in server_text, "remote server has upload/entity/concurrency guards"))

    proc = subprocess.run(
        [sys.executable, str(SERVER), "--host", "0.0.0.0", "--bricscad", str(SERVER)],
        text=True,
        capture_output=True,
        timeout=10,
    )
    results.append(check(proc.returncode != 0 and "BRICSCAD_SERVER_TOKEN" in (proc.stderr + proc.stdout), "non-loopback server refuses to start without token"))

    scan = re.compile(r"14\.22\.76\.61|cc_|apiKey|usr105|格莱利|高斯咨询|E:\\\\高斯|CAD_READER_UPLOAD_ROOTS|CatsCompany|XiaoBa", re.I)
    leaks = []
    for path in ROOT.rglob("*"):
        if path.is_dir() or path.name.endswith((".pyc",)) or path.name == "test_public_contract.py" or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if scan.search(text):
            leaks.append(str(path.relative_to(ROOT)))
    results.append(check(not leaks, "no private endpoint/customer/CatsCo-specific traces"))

    report = {"ok": all(item["ok"] for item in results), "checks": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

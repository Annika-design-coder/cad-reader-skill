from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


PYTHON = sys.executable
ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "cad_reader_tool.py"


def load_env_files(root: Path) -> None:
    for name in (".env", ".env.local"):
        path = root / name
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run_tool(args: list[str], timeout: int) -> tuple[int, dict[str, Any], str]:
    proc = subprocess.run(
        [PYTHON, str(TOOL), "--work-root", str(ROOT), *args],
        text=True,
        capture_output=True,
        encoding="utf-8",
        timeout=timeout,
    )
    text = proc.stdout.strip() or proc.stderr.strip()
    try:
        payload = json.loads(text)
    except Exception:
        payload = {"ok": False, "raw": text}
    return proc.returncode, payload, text


def check(condition: bool, message: str, details: Any = None) -> dict[str, Any]:
    result = {"ok": bool(condition), "message": message}
    if details is not None:
        result["details"] = details
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run L0-L5 acceptance checks for the generic CAD reader tool.")
    parser.add_argument("--file", help="DWG/DXF/PDF sample drawing. Required for full L0-L5.")
    parser.add_argument("--pattern", default="轴|门|窗|平面|说明", help="Regex used for L3 query.")
    parser.add_argument("--bbox", nargs=4, type=float, help="Known CAD bbox for L2/L4 regional checks.")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--skip-remote", action="store_true", help="Only run local CLI/error-path checks.")
    args = parser.parse_args()

    load_env_files(ROOT)
    if args.skip_remote:
        os.environ["CADLIST_REMOTE_BRICSCAD_URL"] = ""
        os.environ["CADLIST_REMOTE_BRICSCAD_TOKEN"] = ""
    results: list[dict[str, Any]] = []

    code, payload, _ = run_tool(["health"], min(args.timeout, 60))
    results.append(check(
        payload.get("ok") is True or payload.get("endpoint_configured") is True or "CADLIST_REMOTE_BRICSCAD_URL" in json.dumps(payload, ensure_ascii=False),
        "health returns JSON and clear remote status",
        payload,
    ))

    code, payload, _ = run_tool(["inspect", "--file", str(ROOT / "SKILL.md")], 30)
    results.append(check(code != 0 and "unsupported drawing type" in json.dumps(payload, ensure_ascii=False), "unsupported file type reports clear error", payload))

    if args.skip_remote:
        report = {"ok": all(item["ok"] for item in results), "mode": "local", "checks": results}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    if not args.file:
        results.append(check(False, "full L0-L5 requires --file sample drawing"))
        report = {"ok": False, "mode": "full", "checks": results}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    drawing = Path(args.file).resolve()

    code, inspect_payload, _ = run_tool(["inspect", "--file", str(drawing), "--preview"], args.timeout)
    inspect_data = inspect_payload.get("data", {})
    extents = inspect_data.get("extents")
    results.append(check(code == 0 and inspect_payload.get("ok") is True and inspect_data.get("entity_count", 0) >= 0, "L0 inspect extracts file metadata, extents, layers, and entity types", inspect_payload))

    code, render_payload, _ = run_tool(["render", "--file", str(drawing)], args.timeout)
    preview = Path(render_payload.get("preview_image", ""))
    results.append(check(code == 0 and render_payload.get("ok") is True and preview.exists() and preview.stat().st_size > 1024, "L1 full drawing render produces a non-empty image", render_payload))

    region_bbox = args.bbox or extents
    if region_bbox:
        bbox_args = [str(value) for value in region_bbox]
        code, region_payload, _ = run_tool(["region", "--file", str(drawing), "--bbox", *bbox_args, "--render"], args.timeout)
        results.append(check(code == 0 and region_payload.get("ok") is True and "evidence" in region_payload, "L2 regional render/inspect returns local evidence", region_payload))

        code, measure_payload, _ = run_tool(["measure", "--file", str(drawing), "--bbox", *bbox_args], args.timeout)
        results.append(check(code == 0 and measure_payload.get("ok") is True and "data" in measure_payload, "L4 regional counting/measurement returns verifiable totals", measure_payload))
    else:
        results.append(check(False, "L2/L4 skipped because no bbox/extents were available"))

    code, query_payload, _ = run_tool(["query", "--file", str(drawing), "--pattern", args.pattern, "--regex"], args.timeout)
    results.append(check(code == 0 and query_payload.get("ok") is True and "evidence" in query_payload, "L3 structured query returns matches or an empty evidence packet", query_payload))

    code, diagnose_payload, _ = run_tool(["diagnose", "--file", str(drawing)], args.timeout)
    results.append(check(code == 0 and diagnose_payload.get("ok") is True and "evidence" in diagnose_payload, "L5 diagnosis returns evidence-backed risk summary", diagnose_payload))

    report = {"ok": all(item["ok"] for item in results), "mode": "full", "checks": results}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

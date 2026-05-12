from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BRICSCAD = "/opt/bricsys/bricscad/v26/bricscad.sh"


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def command_exists(name: str) -> str:
    return shutil.which(name) or ""


def port_connect(host: str, port: int, timeout: float = 3.0) -> dict[str, Any]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def http_json(url: str, token: str = "", timeout: int = 5) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except Exception:
                payload = {"raw": body}
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "response": payload}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"raw": body}
        return {"ok": False, "status": exc.code, "response": payload}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": f"{type(exc).__name__}: {exc}"}


def run_capture(command: list[str], timeout: int = 10) -> dict[str, Any]:
    try:
        proc = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip()[-2000:],
            "stderr": proc.stderr.strip()[-2000:],
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def recommendations(checks: dict[str, Any]) -> list[str]:
    tips = []
    if not checks["python"]["ok"]:
        tips.append("Install or expose python3 on the server PATH.")
    if not checks["bricscad_path"]["exists"]:
        tips.append("Set BRICSCAD_CMD to the real BricsCAD Linux executable path.")
    if not checks["lisp_path"]["exists"]:
        tips.append("Deploy remote_bricscad_export.lsp and set BRICSCAD_LISP_PATH to it.")
    if not checks["xvfb"]["path"] and not checks["display"]["value"]:
        tips.append("Install xvfb or provide DISPLAY; headless BricsCAD usually needs xvfb-run.")
    if checks["local_health"]["status"] == 0 and checks["port"]["ok"]:
        tips.append("Port is open but HTTP /health closes or resets. The listener is likely not remote_bricscad_server.py, or a proxy is dropping requests.")
    if not checks["port"]["ok"]:
        tips.append("Start the systemd service or check firewall/security group for the configured port.")
    if checks["local_ready"].get("status") == 503:
        tips.append("/ready reports not ready; inspect bricscad_exists and lisp_exists fields.")
    return tips


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose a remote BricsCAD reader server from the server host.")
    parser.add_argument("--host", default=os.environ.get("BRICSCAD_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BRICSCAD_SERVER_PORT", "8765")))
    parser.add_argument("--public-url", default=os.environ.get("CADLIST_REMOTE_BRICSCAD_URL", ""))
    parser.add_argument("--token", default=os.environ.get("BRICSCAD_SERVER_TOKEN", ""))
    parser.add_argument("--bricscad", default=os.environ.get("BRICSCAD_CMD", DEFAULT_BRICSCAD))
    parser.add_argument("--lisp", default=os.environ.get("BRICSCAD_LISP_PATH", str(Path(__file__).with_name("remote_bricscad_export.lsp"))))
    args = parser.parse_args()

    local_url = f"http://127.0.0.1:{args.port}"
    public_url = args.public_url.rstrip("/")
    bricscad = Path(args.bricscad)
    lisp = Path(args.lisp)

    checks = {
        "python": run_capture([sys.executable, "--version"], timeout=5),
        "bricscad_path": {"path": str(bricscad), "exists": bricscad.exists()},
        "lisp_path": {"path": str(lisp), "exists": lisp.exists()},
        "xvfb": {"path": command_exists("xvfb-run")},
        "display": {"value": os.environ.get("DISPLAY", "")},
        "port": port_connect("127.0.0.1", args.port),
        "local_health": http_json(local_url + "/health", token=args.token),
        "local_ready": http_json(local_url + "/ready", token=args.token),
        "public_health": http_json(public_url + "/health", token=args.token) if public_url else {"ok": None, "skipped": "no --public-url"},
    }

    report = {
        "ok": bool(checks["local_health"].get("ok") and checks["local_ready"].get("ok")),
        "checks": checks,
        "recommendations": recommendations(checks),
    }
    print_json(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

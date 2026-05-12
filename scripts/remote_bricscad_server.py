from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


DEFAULT_BRICSCAD = "/opt/bricsys/bricscad/v26/bricscad.sh"
DEFAULT_TEMPLATE = '"{bricscad}" -automation -b "{script}"'
DEFAULT_MAX_UPLOAD_MB = 100
DEFAULT_MAX_ENTITY_LIMIT = 100000
DEFAULT_MAX_CONCURRENT = 2


def public_error(exc: Exception) -> dict[str, Any]:
    if os.environ.get("BRICSCAD_DEBUG_ERRORS", "").lower() in {"1", "true", "yes", "on"}:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
    return {
        "ok": False,
        "error": "remote BricsCAD request failed",
        "error_type": type(exc).__name__,
        "hint": "Check the remote service journal for details, or set BRICSCAD_DEBUG_ERRORS=1 in a private environment.",
    }


def to_cad_path(path: Path) -> str:
    return str(path).replace("\\", "/")


def is_loopback_host(host: str) -> bool:
    normalized = (host or "").strip().lower()
    return normalized in {"127.0.0.1", "localhost", "::1"}


def clamp_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, min(parsed, maximum))


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    match = re.search(r"boundary=(?P<boundary>[^;]+)", content_type or "")
    if not match:
        raise ValueError("missing multipart boundary")
    boundary = match.group("boundary").strip('"')
    marker = ("--" + boundary).encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for raw in body.split(marker):
        raw = raw.strip()
        if not raw or raw == b"--":
            continue
        if raw.endswith(b"--"):
            raw = raw[:-2].strip()
        header_blob, _, data = raw.partition(b"\r\n\r\n")
        if not header_blob:
            continue
        headers = header_blob.decode("utf-8", errors="replace")
        if data.endswith(b"\r\n"):
            data = data[:-2]
        disposition = next((line for line in headers.splitlines() if line.lower().startswith("content-disposition:")), "")
        name_match = re.search(r'name="([^"]+)"', disposition)
        if not name_match:
            continue
        name = name_match.group(1)
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        if filename_match:
            files[name] = (filename_match.group(1) or "drawing.dwg", data)
        else:
            fields[name] = data.decode("utf-8", errors="replace")
    return fields, files


def clean_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("texts", "dimensions", "tables", "entities", "block_references"):
        value = payload.get(key)
        if isinstance(value, list):
            payload[key] = [item for item in value if item is not None]
    payload.setdefault("reader", {})
    payload["reader"]["name"] = "remote-bricscad-native"
    payload["reader"]["version"] = "BricsCAD Linux"
    payload["reader"]["status"] = payload["reader"].get("status") or payload.get("status") or "ok"
    return payload


def read_bricscad_json(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    text = ""
    for encoding in ("utf-8-sig", "gb18030", "cp936"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = data.decode("latin-1", errors="replace")
    text = re.sub(r"\\U\+([0-9A-Fa-f]{4})", r"\\u\1", text)
    return json.loads(text)


def safe_temp_cad_name(filename: str) -> str:
    base = Path(filename).name or "drawing.dwg"
    stem = re.sub(r"[^A-Za-z0-9_.#-]+", "_", base).strip("._")
    if not stem:
        stem = "drawing.dwg"
    if Path(stem).suffix.lower() not in {".dwg", ".dxf", ".pdf"}:
        stem += ".dwg"
    return stem


def run_bricscad(bricscad: str, template: str, dwg: Path, script: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    use_xvfb = os.environ.get("BRICSCAD_USE_XVFB", "auto").lower()
    should_wrap = use_xvfb in {"1", "true", "yes", "on"}
    if use_xvfb == "auto":
        should_wrap = not os.environ.get("DISPLAY") and shutil.which("xvfb-run") is not None
    if should_wrap and "xvfb-run" not in template:
        template = f"xvfb-run -a {template}"
    command = template.format(
        bricscad=bricscad,
        dwg=str(dwg),
        script=str(script),
    )
    return subprocess.run(
        shlex.split(command),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def bricscad_output_tail(proc: subprocess.CompletedProcess[str], limit: int = 1200) -> str:
    text = "\n".join(part for part in [proc.stdout, proc.stderr] if part)
    return text[-limit:] if text else f"BricsCAD exited {proc.returncode}"


def wait_for_file(path: Path, process: subprocess.Popen[str], timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=3)
            raise RuntimeError((stderr or stdout or f"BricsCAD exited {process.returncode}")[-1200:])
        time.sleep(0.25)
    raise TimeoutError(f"BricsCAD preview did not become ready within {timeout} seconds")


def start_xvfb() -> tuple[subprocess.Popen[str], str]:
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise RuntimeError("Xvfb is required for headless preview screenshots")
    base = 90 + (os.getpid() % 100)
    for offset in range(80):
        display = f":{base + offset}"
        proc = subprocess.Popen(
            [xvfb, display, "-screen", "0", "1920x1200x24", "-nolisten", "tcp", "-ac"],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.35)
        if proc.poll() is None:
            return proc, display
    raise RuntimeError("could not start Xvfb display for preview")


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_bricscad_screenshot(bricscad: str, script: Path, ready: Path, output_png: Path, timeout: int) -> None:
    scrot = shutil.which("scrot")
    if not scrot:
        raise RuntimeError("scrot is required for headless preview screenshots")
    xvfb_proc, display = start_xvfb()
    env = os.environ.copy()
    env["DISPLAY"] = display
    env.pop("XAUTHORITY", None)
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            [bricscad, "-automation", "-b", str(script)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        wait_for_file(ready, proc, min(timeout, 180))
        settle_seconds = float(os.environ.get("BRICSCAD_PREVIEW_SETTLE_SECONDS", "30"))
        time.sleep(max(0.5, settle_seconds))
        shot = subprocess.run(
            [scrot, str(output_png)],
            text=True,
            capture_output=True,
            timeout=20,
            env=env,
        )
        if shot.returncode != 0:
            raise RuntimeError((shot.stderr or shot.stdout or "scrot failed")[-1200:])
    finally:
        if proc is not None:
            stop_process(proc)
        stop_process(xvfb_proc)


def lisp_string(value: str) -> str:
    return '"' + value.replace("\\", "/").replace('"', '\\"') + '"'


def write_runner_script(script: Path, lisp: Path, dwg: Path, lines: list[str]) -> None:
    script.write_text(
        "\n".join([
            '(setvar "FILEDIA" 0)',
            '(setvar "CMDDIA" 0)',
            '(setvar "SECURELOAD" 0)',
            f'(command "_.OPEN" {lisp_string(to_cad_path(dwg))})',
            f'(load {lisp_string(to_cad_path(lisp))})',
            *lines,
            '(command "_.QUIT" "_N")',
            "",
        ]),
        encoding="utf-8",
    )


def write_preview_script(script: Path, dwg: Path, ready: Path, min_x: float, min_y: float, max_x: float, max_y: float) -> None:
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    pad_x = max(width * 0.04, 10.0)
    pad_y = max(height * 0.04, 10.0)
    n_min_x = -width / 2.0 - pad_x
    n_min_y = -height / 2.0 - pad_y
    n_max_x = width / 2.0 + pad_x
    n_max_y = height / 2.0 + pad_y
    script.write_text(
        "\n".join([
            '(setvar "FILEDIA" 0)',
            '(setvar "CMDDIA" 0)',
            '(setvar "SECURELOAD" 0)',
            f'(command "_.OPEN" {lisp_string(to_cad_path(dwg))})',
            '(vl-catch-all-apply \'(lambda () (setvar "TILEMODE" 1)))',
            '(vl-catch-all-apply \'(lambda () (setvar "BKGCOLOR" "RGB:24,25,28")))',
            '(vl-catch-all-apply \'(lambda () (setvar "BKGCOLORPS" "RGB:24,25,28")))',
            '(vl-catch-all-apply \'(lambda () (setvar "PAPERBACKGROUND" 0)))',
            '(vl-catch-all-apply \'(lambda () (command "_.-LAYER" "_ON" "*" "_THAW" "*" "_UNLOCK" "*" "")))',
            f'(setq cadlist-preview-p1 (list {min_x:.8f} {min_y:.8f} 0.0))',
            f'(setq cadlist-preview-p2 (list {max_x:.8f} {max_y:.8f} 0.0))',
            f'(setq cadlist-preview-base (list {center_x:.8f} {center_y:.8f} 0.0))',
            '(vl-catch-all-apply \'(lambda () (command "_.UCS" "_W")))',
            '(setq cadlist-preview-mask-ss (ssget "_X" (list (cons 0 "HATCH,SOLID,TRACE,WIPEOUT"))))',
            '(if cadlist-preview-mask-ss (vl-catch-all-apply \'(lambda () (command "_.ERASE" cadlist-preview-mask-ss ""))))',
            '(setq cadlist-preview-ss (ssget "_X" (list (cons 0 "LINE,LWPOLYLINE,POLYLINE,CIRCLE,ARC,ELLIPSE,SPLINE,TEXT,MTEXT,DIMENSION,INSERT,POINT"))))',
            '(if cadlist-preview-ss (vl-catch-all-apply \'(lambda () (command "_.MOVE" cadlist-preview-ss "" cadlist-preview-base (list 0.0 0.0 0.0)))))',
            '(if cadlist-preview-ss (vl-catch-all-apply \'(lambda () (command "_.CHPROP" cadlist-preview-ss "" "_Color" "7" ""))))',
            f'(command "_.ZOOM" "_W" (list {n_min_x:.8f} {n_min_y:.8f} 0.0) (list {n_max_x:.8f} {n_max_y:.8f} 0.0))',
            '(if (not cadlist-preview-ss) (vl-catch-all-apply \'(lambda () (command "_.ZOOM" "_E"))))',
            '(command "_.REGENALL")',
            f'(setq codex-ready-file (open {lisp_string(to_cad_path(ready))} "w"))',
            '(if codex-ready-file (progn (write-line "ready" codex-ready-file) (close codex-ready-file)))',
            '(command "_.DELAY" "60000")',
            "",
        ]),
        encoding="utf-8",
    )


class BricsCadHandler(BaseHTTPRequestHandler):
    server_version = "CADListRemoteBricsCAD/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            json.dumps(
                {
                    "event": "http_request",
                    "client": self.client_address[0] if self.client_address else "",
                    "method": self.command,
                    "path": self.path,
                    "message": fmt % args,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )

    def json_response(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def service_status(self) -> dict[str, Any]:
        bricscad = Path(self.server.bricscad)  # type: ignore[attr-defined]
        lisp = Path(self.server.lisp)  # type: ignore[attr-defined]
        return {
            "ok": True,
            "reader": "remote-bricscad-native",
            "server_version": self.server_version,
            "bricscad_exists": bricscad.exists(),
            "lisp_exists": lisp.exists(),
            "token_required": bool(self.server.token),  # type: ignore[attr-defined]
            "timeout": self.server.timeout,  # type: ignore[attr-defined]
            "xvfb_available": bool(shutil.which("xvfb-run")),
            "display_configured": bool(os.environ.get("DISPLAY", "")),
        }

    def check_auth(self) -> bool:
        token = self.server.token  # type: ignore[attr-defined]
        if not token:
            return True
        auth = self.headers.get("Authorization", "")
        header_token = self.headers.get("X-CADLIST-TOKEN", "")
        if auth == f"Bearer {token}" or header_token == token:
            return True
        self.json_response(401, {"ok": False, "error": "unauthorized"})
        return False

    def do_GET(self) -> None:
        if self.path == "/health":
            self.json_response(200, self.service_status())
            return
        if self.path == "/ready":
            status = self.service_status()
            ready = bool(status["bricscad_exists"] and status["lisp_exists"])
            status["ok"] = ready
            self.json_response(200 if ready else 503, status)
            return
        self.json_response(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self.check_auth():
            return
        semaphore = self.server.semaphore  # type: ignore[attr-defined]
        if not semaphore.acquire(blocking=False):
            self.json_response(429, {"ok": False, "error": "remote BricsCAD service is busy"})
            return
        try:
            self._do_post_with_slot()
        finally:
            semaphore.release()

    def _do_post_with_slot(self) -> None:
        if self.path not in {"/v1/dwg/extract", "/v1/dwg/preview"}:
            self.json_response(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            max_upload_bytes = self.server.max_upload_bytes  # type: ignore[attr-defined]
            if length <= 0:
                self.json_response(400, {"ok": False, "error": "missing request body"})
                return
            if length > max_upload_bytes:
                self.json_response(413, {"ok": False, "error": "upload too large", "max_upload_bytes": max_upload_bytes})
                return
            fields, files = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
            if "dwg" not in files:
                raise ValueError("missing dwg file")
            filename, dwg_bytes = files["dwg"]
            timeout = clamp_int(fields.get("timeout") or "", self.server.timeout, 1, self.server.timeout)  # type: ignore[attr-defined]
            with tempfile.TemporaryDirectory(prefix="cadlist_bricscad_") as tmp:
                tmpdir = Path(tmp)
                dwg = tmpdir / safe_temp_cad_name(filename)
                dwg.write_bytes(dwg_bytes)
                lisp_src = Path(self.server.lisp)  # type: ignore[attr-defined]
                lisp = tmpdir / "remote_bricscad_export.lsp"
                lisp.write_text(lisp_src.read_text(encoding="utf-8"), encoding="utf-8")
                script = tmpdir / "run.scr"

                if self.path == "/v1/dwg/extract":
                    output_json = tmpdir / "extract.json"
                    entity_limit = clamp_int(fields.get("entity_limit") or "30000", 30000, 1, self.server.max_entity_limit)  # type: ignore[attr-defined]
                    preview_png = tmpdir / "preview.png"
                    lines = [
                        f'(codex-export-dwg-json "{to_cad_path(output_json)}" {entity_limit})',
                    ]
                    write_runner_script(script, lisp, dwg, lines)
                    proc = run_bricscad(self.server.bricscad, self.server.template, dwg, script, timeout)  # type: ignore[attr-defined]
                    if proc.returncode != 0 and not output_json.exists():
                        raise RuntimeError((proc.stderr or proc.stdout or f"BricsCAD exited {proc.returncode}")[-1200:])
                    if not output_json.exists():
                        raise RuntimeError("BricsCAD did not produce extraction JSON")
                    payload = clean_payload(read_bricscad_json(output_json))
                    response: dict[str, Any] = {"ok": True, "payload": payload}
                    if preview_png.exists() and preview_png.stat().st_size > 1024:
                        response["preview_png_base64"] = base64.b64encode(preview_png.read_bytes()).decode("ascii")
                    self.json_response(200, response)
                    return

                min_x = float(fields["min_x"])
                min_y = float(fields["min_y"])
                max_x = float(fields["max_x"])
                max_y = float(fields["max_y"])
                preview_png = tmpdir / "preview.png"
                ready_file = tmpdir / "preview.ready"
                write_preview_script(script, dwg, ready_file, min_x, min_y, max_x, max_y)
                run_bricscad_screenshot(self.server.bricscad, script, ready_file, preview_png, timeout)  # type: ignore[attr-defined]
                if not preview_png.exists() or preview_png.stat().st_size <= 1024:
                    raise RuntimeError("BricsCAD did not produce preview PNG")
                self.json_response(200, {
                    "ok": True,
                    "preview_strategy": "suppress_fill_masks_high_contrast",
                    "preview_png_base64": base64.b64encode(preview_png.read_bytes()).decode("ascii"),
                })
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "event": "request_error",
                        "path": self.path,
                        "error": str(exc),
                        "traceback": traceback.format_exc(limit=8),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
                flush=True,
            )
            self.json_response(500, public_error(exc))


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="HTTP DWG reader service backed by BricsCAD on Linux.")
    parser.add_argument("--host", default=os.environ.get("BRICSCAD_SERVER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BRICSCAD_SERVER_PORT", "8765")))
    parser.add_argument("--bricscad", default=os.environ.get("BRICSCAD_CMD", DEFAULT_BRICSCAD))
    parser.add_argument("--template", default=os.environ.get("BRICSCAD_ARGS_TEMPLATE", DEFAULT_TEMPLATE))
    parser.add_argument("--token", default=os.environ.get("BRICSCAD_SERVER_TOKEN", ""))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("BRICSCAD_TIMEOUT", "900")))
    parser.add_argument("--max-upload-mb", type=int, default=int(os.environ.get("BRICSCAD_MAX_UPLOAD_MB", str(DEFAULT_MAX_UPLOAD_MB))))
    parser.add_argument("--max-entity-limit", type=int, default=int(os.environ.get("BRICSCAD_MAX_ENTITY_LIMIT", str(DEFAULT_MAX_ENTITY_LIMIT))))
    parser.add_argument("--max-concurrent", type=int, default=int(os.environ.get("BRICSCAD_MAX_CONCURRENT", str(DEFAULT_MAX_CONCURRENT))))
    parser.add_argument("--lisp", default=os.environ.get("BRICSCAD_LISP_PATH", str(Path(__file__).with_name("remote_bricscad_export.lsp"))))
    args = parser.parse_args()

    if not args.token and not is_loopback_host(args.host):
        raise SystemExit("BRICSCAD_SERVER_TOKEN is required when listening on a non-loopback host")
    if args.max_upload_mb < 1:
        raise SystemExit("BRICSCAD_MAX_UPLOAD_MB must be at least 1")
    if args.max_entity_limit < 1:
        raise SystemExit("BRICSCAD_MAX_ENTITY_LIMIT must be at least 1")
    if args.max_concurrent < 1:
        raise SystemExit("BRICSCAD_MAX_CONCURRENT must be at least 1")

    lisp = Path(args.lisp)
    if not lisp.exists():
        raise SystemExit(f"LISP exporter not found: {lisp}")
    if not Path(args.bricscad).exists():
        raise SystemExit(f"BricsCAD command not found: {args.bricscad}")

    server = ReusableThreadingHTTPServer((args.host, args.port), BricsCadHandler)
    server.bricscad = args.bricscad  # type: ignore[attr-defined]
    server.template = args.template  # type: ignore[attr-defined]
    server.token = args.token  # type: ignore[attr-defined]
    server.timeout = args.timeout  # type: ignore[attr-defined]
    server.max_upload_bytes = args.max_upload_mb * 1024 * 1024  # type: ignore[attr-defined]
    server.max_entity_limit = args.max_entity_limit  # type: ignore[attr-defined]
    server.semaphore = threading.BoundedSemaphore(args.max_concurrent)  # type: ignore[attr-defined]
    server.lisp = str(lisp)  # type: ignore[attr-defined]
    print(f"Remote BricsCAD reader listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path


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
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


def build_multipart(dwg: Path, extra_fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = "----cadlist-" + uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in extra_fields.items():
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    content_type = mimetypes.guess_type(dwg.name)[0] or "application/octet-stream"
    parts.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="dwg"; filename="{dwg.name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        dwg.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(parts), boundary


def request_json(
    url: str,
    token: str,
    timeout: int,
    dwg: Path | None = None,
    endpoint: str = "/health",
    fields: dict[str, str] | None = None,
) -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if dwg:
        data, boundary = build_multipart(dwg, {"timeout": str(timeout), **(fields or {})})
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        method = "POST"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url.rstrip("/") + endpoint, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"ok": False, "error": body}
        return exc.code, payload
    except Exception as exc:
        return 0, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Test a remote BricsCAD DWG reader service.")
    parser.add_argument("--url", default=os.environ.get("CADLIST_REMOTE_BRICSCAD_URL", ""))
    parser.add_argument("--token", default=os.environ.get("CADLIST_REMOTE_BRICSCAD_TOKEN", ""))
    parser.add_argument("--dwg", help="Optional DWG file for real extraction test.")
    parser.add_argument("--work-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("CADLIST_REMOTE_BRICSCAD_TIMEOUT", "120")))
    parser.add_argument("--preview", action="store_true", help="Also test /v1/dwg/preview. Requires --dwg.")
    args = parser.parse_args()

    work_root = Path(args.work_root).resolve()
    load_env_files(work_root)
    url = args.url or os.environ.get("CADLIST_REMOTE_BRICSCAD_URL", "")
    token = args.token or os.environ.get("CADLIST_REMOTE_BRICSCAD_TOKEN", "")
    if not url:
        print(json.dumps({"ok": False, "error": "missing CADLIST_REMOTE_BRICSCAD_URL"}, ensure_ascii=False, indent=2))
        return 2

    report: dict[str, object] = {"url": url, "has_token": bool(token), "checks": []}
    status, payload = request_json(url, token, args.timeout, endpoint="/health")
    report["checks"].append({"name": "health", "status": status, "ok": status == 200 and payload.get("ok") is True, "response": payload})

    if args.dwg:
        dwg = Path(args.dwg).resolve()
        if not dwg.exists():
            report["checks"].append({"name": "extract", "ok": False, "error": f"DWG not found: {dwg}"})
        else:
            status, payload = request_json(
                url,
                token,
                args.timeout,
                dwg=dwg,
                endpoint="/v1/dwg/extract",
                fields={"entity_limit": "5000", "preview": "0"},
            )
            report["checks"].append({
                "name": "extract",
                "status": status,
                "ok": status == 200 and payload.get("ok") is True and isinstance(payload.get("payload"), dict),
                "response_keys": sorted(payload.keys()),
                "reader": ((payload.get("payload") or {}).get("reader") or {}) if isinstance(payload.get("payload"), dict) else None,
                "error": payload.get("error"),
            })
            if args.preview:
                status, payload = request_json(
                    url,
                    token,
                    args.timeout,
                    dwg=dwg,
                    endpoint="/v1/dwg/preview",
                    fields={"min_x": "-1e99", "min_y": "-1e99", "max_x": "1e99", "max_y": "1e99"},
                )
                preview = payload.get("preview_png_base64")
                report["checks"].append({
                    "name": "preview",
                    "status": status,
                    "ok": status == 200 and payload.get("ok") is True and isinstance(preview, str) and len(preview) > 1024,
                    "preview_base64_length": len(preview) if isinstance(preview, str) else 0,
                    "error": payload.get("error"),
                })

    all_ok = all(bool(item.get("ok")) for item in report["checks"])  # type: ignore[union-attr]
    report["ok"] = all_ok
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

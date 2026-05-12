from __future__ import annotations

import argparse
import base64
import binascii
import struct
import zlib
import hashlib
import json
import math
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


SUPPORTED_EXTENSIONS = {".dwg", ".dxf", ".pdf"}
TEXT_TYPES = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
# Public skill builds do not ship a shared CAD service endpoint or token.
# Configure CADLIST_REMOTE_BRICSCAD_URL and CADLIST_REMOTE_BRICSCAD_TOKEN in
# .env.local, or deploy scripts/remote_bricscad_server.py on your own server.


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


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


def print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_bytes(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def decode_png_rgba(path: Path) -> tuple[int, int, list[bytes]] | None:
    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    pos = 8
    width = height = bit_depth = color_type = None
    compressed = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        chunk_type = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if chunk_type == b"IHDR":
            width, height, bit_depth, color_type, _compression, _filter, _interlace = struct.unpack(">IIBBBBB", chunk_data)
        elif chunk_type == b"IDAT":
            compressed.extend(chunk_data)
        elif chunk_type == b"IEND":
            break

    if not width or not height or bit_depth != 8 or color_type not in {0, 2, 6}:
        return None

    channels = {0: 1, 2: 3, 6: 4}[color_type]
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(compressed))
    except (zlib.error, binascii.Error):
        return None

    rows: list[bytes] = []
    previous = [0] * stride
    offset = 0
    for _y in range(height):
        if offset >= len(raw):
            return None
        filter_type = raw[offset]
        offset += 1
        scanline = list(raw[offset:offset + stride])
        offset += stride
        if len(scanline) != stride:
            return None

        for i, value in enumerate(scanline):
            left = scanline[i - channels] if i >= channels else 0
            up = previous[i]
            up_left = previous[i - channels] if i >= channels else 0
            if filter_type == 1:
                scanline[i] = (value + left) & 0xFF
            elif filter_type == 2:
                scanline[i] = (value + up) & 0xFF
            elif filter_type == 3:
                scanline[i] = (value + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                p = left + up - up_left
                pa = abs(p - left)
                pb = abs(p - up)
                pc = abs(p - up_left)
                predictor = left if pa <= pb and pa <= pc else up if pb <= pc else up_left
                scanline[i] = (value + predictor) & 0xFF
            elif filter_type != 0:
                return None

        if channels == 4:
            rows.append(bytes(scanline))
        elif channels == 3:
            rgba = bytearray()
            for i in range(0, len(scanline), 3):
                rgba.extend((scanline[i], scanline[i + 1], scanline[i + 2], 255))
            rows.append(bytes(rgba))
        else:
            rgba = bytearray()
            for value in scanline:
                rgba.extend((value, value, value, 255))
            rows.append(bytes(rgba))
        previous = scanline

    return width, height, rows


def is_uninformative_bricscad_preview(path: Path) -> tuple[bool, dict[str, Any]]:
    decoded = decode_png_rgba(path)
    if not decoded:
        return False, {"blank_check": "unsupported_png"}
    width, height, rows = decoded
    if width < 400 or height < 300:
        return False, {"blank_check": "small_image", "width": width, "height": height}

    top_dark = 0
    top_total = 0
    for y in range(0, max(1, int(height * 0.12))):
        row = rows[y]
        for x in range(width):
            i = x * 4
            r, g, b = row[i], row[i + 1], row[i + 2]
            top_dark += 1 if max(r, g, b) < 80 else 0
            top_total += 1

    crop_y1, crop_y2 = int(height * 0.22), int(height * 0.88)
    white_columns: list[bool] = []
    for x in range(width):
        column_white = 0
        column_total = 0
        for y in range(crop_y1, crop_y2):
            row = rows[y]
            i = x * 4
            r, g, b = row[i], row[i + 1], row[i + 2]
            column_total += 1
            if r > 246 and g > 246 and b > 246:
                column_white += 1
        white_columns.append((column_white / max(column_total, 1)) > 0.97)

    best_start = best_end = current_start = 0
    in_run = False
    for index, is_white_column in enumerate(white_columns + [False]):
        if is_white_column and not in_run:
            current_start = index
            in_run = True
        elif not is_white_column and in_run:
            if index - current_start > best_end - best_start:
                best_start, best_end = current_start, index
            in_run = False

    crop_x1, crop_x2 = best_start, best_end
    if crop_x2 - crop_x1 < int(width * 0.20):
        crop_x1, crop_x2 = int(width * 0.25), int(width * 0.58)

    near_white = 0
    dark_or_colored = 0
    total = 0
    for y in range(crop_y1, crop_y2):
        row = rows[y]
        for x in range(crop_x1, crop_x2):
            i = x * 4
            r, g, b = row[i], row[i + 1], row[i + 2]
            total += 1
            if r > 246 and g > 246 and b > 246:
                near_white += 1
            if max(r, g, b) < 220 or (max(r, g, b) - min(r, g, b)) > 28:
                dark_or_colored += 1

    metrics = {
        "blank_check": "ok",
        "width": width,
        "height": height,
        "top_dark_ratio": round(top_dark / max(top_total, 1), 6),
        "white_viewport_x": [crop_x1, crop_x2],
        "white_viewport_width_ratio": round((crop_x2 - crop_x1) / max(width, 1), 6),
        "center_white_ratio": round(near_white / max(total, 1), 6),
        "center_signal_ratio": round(dark_or_colored / max(total, 1), 6),
    }
    blank = (
        metrics["top_dark_ratio"] > 0.45
        and metrics["white_viewport_width_ratio"] > 0.25
        and metrics["center_white_ratio"] > 0.998
        and metrics["center_signal_ratio"] < 0.001
    )
    return blank, metrics


def clean_text(value: str) -> str:
    value = value or ""
    value = re.sub(r"\\U\+([0-9A-Fa-f]{4})", lambda m: chr(int(m.group(1), 16)), value)
    value = value.replace("\\U+00B2", "㎡").replace("\\U+00D7", "×")
    value = value.replace("%%c", "Φ").replace("%%C", "Φ").replace("%%P", "±").replace("%%p", "±")
    value = value.replace("%%d", "°").replace("%%D", "°")
    value = value.replace("\\P", " ").replace("\\p", " ").replace("\\~", " ")
    value = re.sub(r"\\S([^;^/]+)[/^#]([^;]+);", r"\1/\2", value)
    value = re.sub(r"\\[AaCcFfHhLlOoQqTtWw][^;{}]*;", "", value)
    value = re.sub(r"(?i)(^|\s)(q[clmrj]?|c\d+|a\d+|f[^;\s]+|h[\d.x]+|w[\d.x]+);", " ", value)
    value = re.sub(r"\\[{}]", "", value)
    value = value.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", value).strip()


def item_plain_text(item: dict[str, Any]) -> str:
    text_info = item.get("text_info")
    if isinstance(text_info, dict):
        for key in ("plain_text", "vla_text", "raw_text"):
            value = clean_text(str(text_info.get(key) or ""))
            if value:
                return value
    dimension = item.get("dimension")
    if isinstance(dimension, dict):
        for key in ("plain_text", "text_override", "display_text"):
            value = clean_text(str(dimension.get(key) or ""))
            if value:
                return value
    return clean_text(str(item.get("text") or ""))


def item_text_values(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in [item_plain_text(item), clean_text(str(item.get("block_name") or "")), clean_text(str(item.get("layer") or ""))]:
        if value and value not in values:
            values.append(value)
    attrs = item.get("attributes")
    if isinstance(attrs, list):
        for attr in attrs:
            if not isinstance(attr, dict):
                continue
            tag = clean_text(str(attr.get("tag") or ""))
            text = clean_text(str(attr.get("plain_text") or attr.get("text") or ""))
            combined = f"{tag}:{text}" if tag and text else text or tag
            if combined and combined not in values:
                values.append(combined)
    proxy = item.get("proxy")
    if isinstance(proxy, dict):
        for entry in proxy.get("string_values") or []:
            if isinstance(entry, dict):
                value = clean_text(str(entry.get("value") or ""))
                if value and value not in values:
                    values.append(value)
    return values


def proxy_type_hints(item: dict[str, Any]) -> list[str]:
    proxy = item.get("proxy")
    if not isinstance(proxy, dict):
        return []
    values: list[str] = []
    for entry in proxy.get("string_values") or []:
        if not isinstance(entry, dict):
            continue
        value = clean_text(str(entry.get("value") or ""))
        if value and value not in values:
            values.append(value)
    return values


def classify_proxy_family(type_hints: Counter[str]) -> list[dict[str, Any]]:
    families: list[dict[str, Any]] = []
    tangent_hints = {name: count for name, count in type_hints.items() if name.startswith("TDb")}
    if tangent_hints:
        families.append({
            "family": "tangent_t20",
            "vendor": "天正/T20",
            "confidence": 0.9,
            "matched_type_hints": dict(sorted(tangent_hints.items(), key=lambda kv: (-kv[1], kv[0]))),
            "required_runtime": "云端 Windows CAD worker：AutoCAD + 匹配版本 T20 天正插件/Object Enabler",
            "linux_bricscad_status": "blocked_without_vendor_brx_linux_enabler",
            "reason": "TDb* 是天正自定义对象线索；常见 T20 插件面向 Windows AutoCAD，AutoCAD ARX 不能直接加载到 Linux BricsCAD。",
            "next_step": "在云端 Windows 机器安装 AutoCAD 与 T20 插件后，配置 CADLIST_TANGENT_WORKER_URL 再重跑。",
        })
    return families


def proxy_fallback_plan(summary: dict[str, Any]) -> dict[str, Any]:
    families = summary.get("proxy_families") or []
    tangent = summary.get("tangent_worker") or {}
    has_tangent_proxy = any(isinstance(item, dict) and item.get("family") == "tangent_t20" for item in families)
    active = bool(has_tangent_proxy and not (isinstance(tangent, dict) and tangent.get("ok")))
    return {
        "active": active,
        "mode": "tangent_proxy_degraded" if active else "normal",
        "reliable_scope": [
            "普通 CAD 实体的图层、线、圆、弧、多段线、块、普通文字、普通尺寸",
            "已带 bbox/坐标/measurement 的对象，可用于定位、局部渲染、基础计数和量测",
            "图纸整体范围、图层对象数、可读取文字和普通尺寸证据",
        ],
        "blocked_scope": [
            "天正/T20 代理对象内部文字、符号、坐标标注、引线、轴号组等语义",
            "没有 bbox 的代理对象不能稳定定位局部，也不能作为可靠工程量依据",
            "代理对象内部参数不能直接参与最终清单或风险结论，只能作为需转换/复核证据",
        ] if active else [],
        "agent_policy": [
            "先回答可由普通 CAD 对象证明的问题",
            "涉及 TDb* 代理对象时明确标注证据弱点和影响范围",
            "需要精确识别代理内部内容时，要求出图方提供普通 AutoCAD 对象版 DWG 或 PDF",
        ],
        "conversion_required": active,
    }


def parse_number_label(text: str) -> dict[str, Any]:
    text = clean_text(text)
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*(mm|cm|m|km|毫米|厘米|米)?", text, flags=re.I)
    if not match:
        return {}
    return {
        "value": float(match.group(1)),
        "unit": match.group(2) or "",
        "source_text": text,
    }


def safe_filename(value: str, suffix: str = "") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\s]+', "_", value).strip("._")
    return (cleaned or "drawing") + suffix


def file_id(path: Path) -> str:
    digest = hashlib.sha1(str(path.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:12]
    return safe_filename(f"{path.stem}_{digest}")


def output_root(work_root: Path, drawing: Path) -> Path:
    return work_root / "output" / "cad_reader" / file_id(drawing)


def remote_url() -> str:
    urls = remote_urls()
    return urls[0] if urls else ""


def remote_urls() -> list[str]:
    configured = (os.environ.get("CADLIST_REMOTE_BRICSCAD_URL") or "").strip()
    if configured:
        return [configured.rstrip("/")]
    return []


def remote_token() -> str:
    return os.environ.get("CADLIST_REMOTE_BRICSCAD_TOKEN") or os.environ.get("BRICSCAD_SERVER_TOKEN") or ""


def remote_config_source() -> str:
    if os.environ.get("CADLIST_REMOTE_BRICSCAD_URL"):
        return "environment"
    return "unconfigured"


def remote_timeout(default: int = 180) -> int:
    try:
        return int(os.environ.get("CADLIST_REMOTE_BRICSCAD_TIMEOUT", str(default)))
    except ValueError:
        return default


def tangent_worker_url() -> str:
    return (os.environ.get("CADLIST_TANGENT_WORKER_URL") or "").rstrip("/")


def tangent_worker_token() -> str:
    return os.environ.get("CADLIST_TANGENT_WORKER_TOKEN") or ""


def tangent_worker_timeout(default: int = 900) -> int:
    try:
        return int(os.environ.get("CADLIST_TANGENT_WORKER_TIMEOUT", str(default)))
    except ValueError:
        return default


def classify_file(path: Path) -> dict[str, Any]:
    ext = path.suffix.lower()
    return {
        "path": str(path),
        "name": path.name,
        "extension": ext,
        "supported": ext in SUPPORTED_EXTENSIONS,
        "kind": {".dwg": "DWG", ".dxf": "DXF", ".pdf": "PDF"}.get(ext, "UNKNOWN"),
        "size": path.stat().st_size if path.exists() else None,
    }


def bbox_from_entity(item: dict[str, Any]) -> list[float] | None:
    bbox = item.get("bbox")
    if isinstance(bbox, dict) and bbox.get("min") and bbox.get("max"):
        mn = bbox["min"]
        mx = bbox["max"]
        return [float(mn[0]), float(mn[1]), float(mx[0]), float(mx[1])]
    geom = item.get("geometry")
    if isinstance(geom, dict):
        points = geom.get("points")
        if isinstance(points, list):
            pts = [p for p in points if isinstance(p, list) and len(p) >= 2]
            if pts:
                xs = [float(p[0]) for p in pts]
                ys = [float(p[1]) for p in pts]
                return [min(xs), min(ys), max(xs), max(ys)]
        center = geom.get("center")
        if isinstance(center, list) and len(center) >= 2:
            radius = float(geom.get("radius") or 0)
            return [float(center[0]) - radius, float(center[1]) - radius, float(center[0]) + radius, float(center[1]) + radius]
    pos = item.get("position")
    if isinstance(pos, list) and len(pos) >= 2:
        return [float(pos[0]), float(pos[1]), float(pos[0]), float(pos[1])]
    return None


def normalize_bbox(values: list[float]) -> list[float]:
    x1, y1, x2, y2 = [float(v) for v in values]
    return [min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)]


def bbox_intersects(a: list[float], b: list[float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def merge_bbox(a: list[float] | None, b: list[float] | None) -> list[float] | None:
    if not b:
        return a
    if not a:
        return b
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def pad_bbox(bbox: list[float], ratio: float = 0.18, minimum: float = 1000.0) -> list[float]:
    w = max(bbox[2] - bbox[0], 1.0)
    h = max(bbox[3] - bbox[1], 1.0)
    px = max(w * ratio, minimum)
    py = max(h * ratio, minimum)
    return [bbox[0] - px, bbox[1] - py, bbox[2] + px, bbox[3] + py]


def in_bbox(item: dict[str, Any], bbox: list[float] | None) -> bool:
    if bbox is None:
        return True
    item_bbox = bbox_from_entity(item)
    return bool(item_bbox and bbox_intersects(item_bbox, bbox))


def list_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    values = payload.get(key, [])
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def numeric(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def distance(p1: list[float], p2: list[float]) -> float:
    return math.hypot(float(p2[0]) - float(p1[0]), float(p2[1]) - float(p1[1]))


def polyline_area(points: list[list[float]]) -> float:
    if len(points) < 3:
        return 0.0
    total = 0.0
    for idx, point in enumerate(points):
        nxt = points[(idx + 1) % len(points)]
        total += float(point[0]) * float(nxt[1]) - float(nxt[0]) * float(point[1])
    return abs(total) / 2


def inferred_length_area(item: dict[str, Any]) -> tuple[float, float]:
    length = numeric(item.get("length")) or 0.0
    area = numeric(item.get("area")) or 0.0
    geom = item.get("geometry")
    if not isinstance(geom, dict):
        return length, area
    gtype = geom.get("type")
    if gtype == "line" and not length:
        pts = geom.get("points") or []
        if len(pts) >= 2:
            length = distance(pts[0], pts[1])
    elif gtype == "polyline":
        pts = [p for p in geom.get("points") or [] if isinstance(p, list) and len(p) >= 2]
        if pts and not length:
            length = sum(distance(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            if item.get("closed") and len(pts) > 2:
                length += distance(pts[-1], pts[0])
        if pts and item.get("closed") and not area:
            area = polyline_area(pts)
    elif gtype == "circle":
        radius = numeric(geom.get("radius")) or 0.0
        length = length or 2 * math.pi * radius
        area = area or math.pi * radius * radius
    elif gtype == "arc" and not length:
        radius = numeric(geom.get("radius")) or 0.0
        start = numeric(geom.get("start")) or 0.0
        end = numeric(geom.get("end")) or 0.0
        if end < start:
            end += math.tau
        length = radius * abs(end - start)
    return length, area


def build_multipart(path: Path, fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = "----cad-reader-" + uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="dwg"; filename="{path.name}"\r\n'.encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(parts), boundary


def remote_request(endpoint: str, drawing: Path | None = None, fields: dict[str, str] | None = None, timeout: int | None = None) -> tuple[int, dict[str, Any]]:
    urls = remote_urls()
    if not urls:
        return 0, {"ok": False, "error": "missing CADLIST_REMOTE_BRICSCAD_URL"}
    final_status = 0
    final_payload: dict[str, Any] = {"ok": False, "error": "no remote CAD endpoint attempted"}
    for url in urls:
        attempt_timeout = timeout or remote_timeout()
        status, payload = remote_request_once(url, endpoint, drawing, fields, attempt_timeout)
        if status == 200 and payload.get("ok") is True:
            return status, payload
        final_status, final_payload = status, payload
    return final_status, final_payload


def remote_request_once(url: str, endpoint: str, drawing: Path | None = None, fields: dict[str, str] | None = None, timeout: int | None = None) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if drawing is not None:
        data, boundary = build_multipart(drawing, {"timeout": str(timeout or remote_timeout()), **(fields or {})})
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        method = "POST"
    token = remote_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url + endpoint, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout or remote_timeout()) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"ok": False, "error": body}
        return exc.code, payload
    except Exception as exc:
        return 0, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def simple_get_json(url: str, token: str = "", timeout: int = 30) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except Exception:
            payload = {"ok": False, "error": body}
        return exc.code, payload
    except Exception as exc:
        return 0, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def tangent_worker_health() -> dict[str, Any]:
    url = tangent_worker_url()
    if not url:
        return {
            "configured": False,
            "ok": False,
            "status": 0,
            "url": "",
            "note": "CADLIST_TANGENT_WORKER_URL is not configured",
        }
    status, payload = simple_get_json(url + "/health", tangent_worker_token(), tangent_worker_timeout(30))
    return {
        "configured": True,
        "ok": status == 200 and payload.get("ok") is True,
        "status": status,
        "url": url,
        "response": payload,
    }


def compact_item(item: dict[str, Any]) -> dict[str, Any]:
    result = {
        "handle": item.get("handle"),
        "object_name": item.get("object_name"),
        "layer": item.get("layer"),
        "bbox": bbox_from_entity(item),
        "position": item.get("position"),
    }
    text = item_plain_text(item)
    if text:
        result["text"] = text
    if isinstance(item.get("text_info"), dict):
        result["text_info"] = {
            key: clean_text(str(item["text_info"].get(key))) if key in {"plain_text", "vla_text", "raw_text"} else item["text_info"].get(key)
            for key in ("plain_text", "style_name", "height", "rotation", "width_factor")
            if item["text_info"].get(key) not in (None, "", "null")
        }
    if item.get("block_name"):
        result["block_name"] = item.get("block_name")
    if isinstance(item.get("attributes"), list) and item["attributes"]:
        result["attributes"] = [
            {
                "tag": attr.get("tag"),
                "text": clean_text(str(attr.get("plain_text") or attr.get("text") or "")),
                "position": attr.get("position"),
                "bbox": bbox_from_entity(attr),
                "invisible": attr.get("invisible"),
            }
            for attr in item["attributes"]
            if isinstance(attr, dict)
        ]
    if isinstance(item.get("dimension"), dict):
        dim_label = parse_number_label(str(item["dimension"].get("plain_text") or item["dimension"].get("text_override") or item["dimension"].get("display_text") or ""))
        result["dimension"] = {
            key: item["dimension"].get(key)
            for key in (
                "measurement",
                "plain_text",
                "text_override",
                "style_name",
                "dimension_type_name",
                "definition_point",
                "text_position",
                "extension_line_1_point",
                "extension_line_2_point",
                "angle",
            )
            if item["dimension"].get(key) not in (None, "", "null")
        }
        if dim_label:
            result["dimension"]["label_value"] = dim_label
    if isinstance(item.get("table"), dict):
        result["table"] = item["table"]
    if isinstance(item.get("proxy"), dict):
        result["proxy"] = {
            "diagnosis": item["proxy"].get("diagnosis"),
            "has_bbox": item["proxy"].get("has_bbox"),
            "binary_chunk_count": item["proxy"].get("binary_chunk_count"),
            "class_markers": item["proxy"].get("class_markers", [])[:6],
            "string_values": item["proxy"].get("string_values", [])[:6],
        }
    if item.get("read_error"):
        result["read_error"] = item["read_error"]
    return result


def payload_summary(payload: dict[str, Any], drawing: Path) -> dict[str, Any]:
    entities = list_items(payload, "entities")
    texts = list_items(payload, "texts")
    dims = list_items(payload, "dimensions")
    tables = list_items(payload, "tables")
    blocks = list_items(payload, "block_references")
    extents = None
    layer_stats: dict[str, dict[str, Any]] = {}
    entity_counts = Counter()
    proxy_count = 0
    proxy_without_bbox_count = 0
    proxy_layers: Counter[str] = Counter()
    proxy_class_markers: Counter[str] = Counter()
    proxy_type_hint_counts: Counter[str] = Counter()
    attribute_count = 0
    attribute_text_count = 0
    text_by_layer: Counter[str] = Counter()
    dimension_measurement_count = 0
    dimension_override_count = 0
    dimension_label_count = 0
    dimension_type_counts: Counter[str] = Counter()
    length_sum = 0.0
    area_sum = 0.0
    for item in entities:
        object_name = str(item.get("object_name") or "UNKNOWN")
        layer = str(item.get("layer") or "(no_layer)")
        bbox = bbox_from_entity(item)
        extents = merge_bbox(extents, bbox)
        length, area = inferred_length_area(item)
        length_sum += length
        area_sum += area
        entity_counts[object_name] += 1
        stat = layer_stats.setdefault(layer, {"entity_count": 0, "object_counts": {}, "bbox": None, "length_sum": 0.0, "area_sum": 0.0})
        stat["entity_count"] += 1
        stat["object_counts"][object_name] = stat["object_counts"].get(object_name, 0) + 1
        stat["bbox"] = merge_bbox(stat["bbox"], bbox)
        stat["length_sum"] += length
        stat["area_sum"] += area
        if "PROXY" in object_name.upper() or "PROXY" in str(item.get("vla_object_name") or "").upper():
            proxy_count += 1
            proxy_layers[layer] += 1
            if not bbox:
                proxy_without_bbox_count += 1
            proxy = item.get("proxy")
            if isinstance(proxy, dict):
                for entry in proxy.get("class_markers") or []:
                    if isinstance(entry, dict) and entry.get("value"):
                        proxy_class_markers[str(entry["value"])] += 1
                for value in proxy_type_hints(item):
                    if value != layer:
                        proxy_type_hint_counts[value] += 1
        text_value = item_plain_text(item)
        if text_value:
            text_by_layer[layer] += 1
        attrs = item.get("attributes")
        if isinstance(attrs, list):
            for attr in attrs:
                if isinstance(attr, dict):
                    attribute_count += 1
                    if clean_text(str(attr.get("plain_text") or attr.get("text") or "")):
                        attribute_text_count += 1
                        text_by_layer[layer] += 1
        dim = item.get("dimension")
        if isinstance(dim, dict):
            if numeric(dim.get("measurement")) is not None:
                dimension_measurement_count += 1
            if clean_text(str(dim.get("text_override") or "")):
                dimension_override_count += 1
            if parse_number_label(str(dim.get("plain_text") or dim.get("text_override") or dim.get("display_text") or "")):
                dimension_label_count += 1
            dimension_type_counts[str(dim.get("dimension_type_name") or "unknown")] += 1
    title_candidates = []
    for item in texts[:500]:
        text = item_plain_text(item)
        if len(text) >= 3 and any(key in text for key in ("图", "表", "说明", "工程", "平面", "立面", "剖面")) and text not in title_candidates:
            title_candidates.append(text)
        if len(title_candidates) >= 20:
            break
    risks = []
    if payload.get("status") != "ok":
        risks.append("云端 CAD 解析未完全成功，结果需要复核")
    if proxy_count:
        risks.append(f"发现 {proxy_count} 个代理对象，可能缺 Object Enabler 或存在自定义对象")
    if proxy_without_bbox_count:
        risks.append(f"{proxy_without_bbox_count} 个代理对象缺少可用 bbox，局部定位和量测需依赖周边文字/图层复核")
    proxy_families = classify_proxy_family(proxy_type_hint_counts)
    if proxy_families:
        risks.append("代理对象疑似来自天正/T20；当前 Linux BricsCAD 缺少对应对象解释器时只能读取代理外壳")
    if not extents:
        risks.append("未能获得有效图纸范围")
    if not texts and attribute_text_count == 0:
        risks.append("未提取到文字，可能是字体/外部参照/扫描 PDF 问题")
    if not dims:
        risks.append("未提取到尺寸标注，量测结论只能基于几何对象")
    summary = {
        "file": classify_file(drawing),
        "status": payload.get("status"),
        "reader": payload.get("reader", {}),
        "document": payload.get("document", {}),
        "units": (payload.get("document") or {}).get("units"),
        "ins_units": (payload.get("document") or {}).get("ins_units"),
        "extents": extents,
        "layouts": payload.get("layouts", []),
        "layer_count": len(payload.get("layers", []) or layer_stats),
        "layers": layer_stats,
        "entity_counts": dict(entity_counts),
        "entity_count": len(entities),
        "text_count": len(texts),
        "text_by_layer": dict(text_by_layer.most_common(80)),
        "text_samples": [compact_item(item) for item in texts[:30]],
        "dimension_count": len(dims),
        "dimension_measurement_count": dimension_measurement_count,
        "dimension_override_count": dimension_override_count,
        "dimension_label_count": dimension_label_count,
        "dimension_type_counts": dict(dimension_type_counts.most_common(20)),
        "dimension_samples": [compact_item(item) for item in dims[:30]],
        "table_count": len(tables),
        "block_reference_count": len(blocks),
        "attribute_count": attribute_count,
        "attribute_text_count": attribute_text_count,
        "block_names": dict(Counter(str(item.get("block_name") or "(anonymous)") for item in blocks).most_common(80)),
        "title_candidates": title_candidates,
        "length_sum": length_sum,
        "area_sum": area_sum,
        "proxy_count": proxy_count,
        "proxy_without_bbox_count": proxy_without_bbox_count,
        "proxy_layers": dict(proxy_layers.most_common(80)),
        "proxy_class_markers": dict(proxy_class_markers.most_common(40)),
        "proxy_type_hints": dict(proxy_type_hint_counts.most_common(80)),
        "proxy_families": proxy_families,
        "tangent_worker": tangent_worker_health() if proxy_families else {"configured": bool(tangent_worker_url())},
        "proxy_samples": [compact_item(item) for item in entities if "PROXY" in str(item.get("object_name") or "").upper()][:20],
        "risks": risks,
    }
    summary["proxy_fallback"] = proxy_fallback_plan(summary)
    return summary


def extraction_paths(work_root: Path, drawing: Path) -> dict[str, Path]:
    root = output_root(work_root, drawing)
    return {
        "root": root,
        "payload": root / "extract.json",
        "summary": root / "summary.json",
        "full_png": root / "full.png",
    }


def ensure_extracted(work_root: Path, drawing: Path, entity_limit: int = 50000, force: bool = False, preview: bool = False) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    paths = extraction_paths(work_root, drawing)
    if paths["payload"].exists() and not force:
        payload = read_json(paths["payload"])
        summary = read_json(paths["summary"]) if paths["summary"].exists() else payload_summary(payload, drawing)
        if "proxy_fallback" not in summary:
            summary = payload_summary(payload, drawing)
            summary["payload_path"] = str(paths["payload"])
            if paths["full_png"].exists():
                summary["full_preview"] = str(paths["full_png"])
            write_json(paths["summary"], summary)
        return payload, summary
    info = classify_file(drawing)
    if not drawing.exists():
        return None, {"ok": False, "error": f"file not found: {drawing}", "file": info}
    if not info["supported"]:
        return None, {"ok": False, "error": f"unsupported drawing type: {info['extension']}", "file": info}
    status, response = remote_request(
        "/v1/dwg/extract",
        drawing,
        {"entity_limit": str(entity_limit), "preview": "1" if preview else "0"},
        timeout=remote_timeout(300),
    )
    if status != 200 or response.get("ok") is not True:
        error = response.get("error") or f"remote CAD service returned HTTP {status}"
        return None, {"ok": False, "error": error, "status": status, "file": info}
    payload = response.get("payload")
    if not isinstance(payload, dict):
        return None, {"ok": False, "error": "remote CAD service returned no structured payload", "status": status, "file": info}
    write_json(paths["payload"], payload)
    if response.get("preview_png_base64"):
        write_bytes(paths["full_png"], base64.b64decode(response["preview_png_base64"]))
    summary = payload_summary(payload, drawing)
    summary["payload_path"] = str(paths["payload"])
    if paths["full_png"].exists():
        summary["full_preview"] = str(paths["full_png"])
    write_json(paths["summary"], summary)
    return payload, summary


def focus_id_for(bbox: list[float] | None, label: str = "focus") -> str:
    if not bbox:
        return label
    raw = ",".join(f"{float(v):.6f}" for v in bbox)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"{label}_{digest}"


def evidence_id_for(drawing: Path, query: dict[str, Any], bbox: list[float] | None) -> str:
    seed = {
        "drawing": str(drawing),
        "query": query,
        "bbox": bbox or [],
        "nonce": uuid.uuid4().hex,
    }
    raw = json.dumps(seed, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def evidence_packet(
    drawing: Path,
    query: dict[str, Any],
    summary: dict[str, Any] | None,
    bbox: list[float] | None = None,
    preview: Path | None = None,
    items: list[dict[str, Any]] | None = None,
    answer: str = "",
    confidence: float = 0.75,
    focus_id: str = "",
    parent_evidence_id: str = "",
    render_mode: str = "",
    evidence_strength: str = "",
    render_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = items or []
    evidence_id = evidence_id_for(drawing, query, bbox)
    strength = evidence_strength or ("strong" if preview and items else "moderate" if preview or items else "weak")
    return {
        "schema": "cad_reader_evidence.v0.1",
        "evidence_id": evidence_id,
        "focus_id": focus_id or focus_id_for(bbox, str(query.get("type") or "focus")),
        "parent_evidence_id": parent_evidence_id,
        "generated_at": now_iso(),
        "drawing": str(drawing),
        "query": query,
        "bbox": bbox or [],
        "previous_bbox": bbox or [],
        "preview_image": str(preview) if preview else "",
        "render_mode": render_mode,
        "render_error": render_error or {},
        "answer": answer,
        "confidence": confidence,
        "evidence_strength": strength,
        "conclusion_tier": strength,
        "summary_path": (summary or {}).get("summary_path", ""),
        "risks": (summary or {}).get("risks", []),
        "texts": [item for item in items if str(item.get("object_name") or "").upper() in TEXT_TYPES],
        "dimensions": [item for item in items if str(item.get("object_name") or "").upper() == "DIMENSION"],
        "blocks": [item for item in items if str(item.get("object_name") or "").upper() == "INSERT"],
        "entities": items,
    }


def remote_error(status: int, response: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": status,
        "error": response.get("error") or response.get("message") or f"remote CAD service returned HTTP {status}",
        "ok": response.get("ok"),
    }


def render_bbox_image(work_root: Path, drawing: Path, payload: dict[str, Any], bbox: list[float], name: str) -> tuple[Path | None, str, dict[str, Any]]:
    out = output_root(work_root, drawing) / safe_filename(name, ".png")
    fields = {"min_x": f"{bbox[0]:.8f}", "min_y": f"{bbox[1]:.8f}", "max_x": f"{bbox[2]:.8f}", "max_y": f"{bbox[3]:.8f}"}
    status, response = remote_request("/v1/dwg/preview", drawing, fields, timeout=remote_timeout(300))
    if status == 200 and response.get("ok") is True and response.get("preview_png_base64"):
        write_bytes(out, base64.b64decode(response["preview_png_base64"]))
        blank, metrics = is_uninformative_bricscad_preview(out)
        if blank:
            try:
                out.unlink()
            except OSError:
                pass
            return None, "remote-bricscad-blank", {
                "status": status,
                "ok": False,
                "error": "remote BricsCAD returned an uninformative blank viewport screenshot",
                "blank_preview_metrics": metrics,
                "recovery_hint": "Render service should normalize very large CAD coordinates near origin before screenshot.",
            }
        render_mode = response.get("preview_strategy") or "remote-bricscad-screenshot"
        return out, render_mode, {"status": status, "ok": True, "preview_strategy": response.get("preview_strategy", "")}
    return None, "remote-bricscad-failed", remote_error(status, response)


def arg_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            parts.extend(str(item).split(","))
        return [part.strip() for part in parts if part.strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def resolve_followup_bbox(args: argparse.Namespace, summary: dict[str, Any] | None) -> list[float] | None:
    if getattr(args, "bbox", None):
        return normalize_bbox(args.bbox)
    if getattr(args, "previous_bbox", None):
        return normalize_bbox(args.previous_bbox)
    return None


def question_terms(question: str) -> list[str]:
    stopwords = {
        "帮我", "看看", "看一下", "这张", "图纸", "施工图", "有没有", "明显", "地方", "问题",
        "风险", "哪里", "什么", "一下", "是否", "检查", "分析", "告诉我", "请问",
    }
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z0-9_.+-]+|[\u4e00-\u9fff]{2,}", question):
        token = token.strip()
        if token and token not in stopwords and token not in terms:
            terms.append(token)
    return terms[:8]


def find_question_matches(payload: dict[str, Any], question: str, limit: int = 20) -> list[dict[str, Any]]:
    terms = question_terms(question)
    if not terms:
        return []
    matches: list[dict[str, Any]] = []
    for item in list_items(payload, "entities"):
        blob = " ".join([*item_text_values(item), str(item.get("object_name") or "")]).lower()
        if any(term.lower() in blob for term in terms):
            matches.append(compact_item(item))
            if len(matches) >= limit:
                break
    return matches


def ask_intent(question: str) -> str:
    if re.search(r"开会|会议|工程方|值得问|问的问题|沟通", question):
        return "meeting"
    if re.search(r"工程咨询|看不太懂|看不懂|先过一遍|过一遍|咨询角度", question):
        return "consulting"
    if re.search(r"能不能直接用|直接用|人工确认|需要确认|能否使用", question):
        return "usability"
    if re.search(r"施工|审查|后续施工|明显不对劲|影响", question):
        return "risk"
    return "default"


def summary_identity(summary: dict[str, Any]) -> str:
    titles = summary.get("title_candidates") or []
    if titles:
        return f"图面标题/候选名称包括：{'、'.join(str(t) for t in titles[:3])}"
    layouts = summary.get("layouts") or []
    if layouts:
        return f"已读取布局：{'、'.join(str(l) for l in layouts[:3])}"
    return "已完成图纸对象读取，但图名/布局名称暂不明确"


def meeting_question_lines(summary: dict[str, Any], risks: list[str]) -> list[str]:
    lines = ["明天开会建议优先问这些问题："]
    lines.append(f"- 版本和范围：{summary_identity(summary)}，请工程方确认这是不是本次讨论的最新出图版本和对应专业范围。")
    if summary.get("units") in (None, "", "Unitless") or not summary.get("ins_units"):
        lines.append("- 单位/比例：当前图纸单位信息不够明确，请确认模型单位、出图比例和是否允许按图面几何直接量测。")
    if summary.get("proxy_count", 0):
        lines.append("- 天正/T20对象：图里存在代理对象，请确认是否能提供普通 AutoCAD 对象版 DWG 或同版 PDF，避免关键文字/尺寸漏读。")
    if summary.get("dimension_count", 0) and summary.get("dimension_measurement_count", 0) < summary.get("dimension_count", 0):
        lines.append("- 尺寸依据：部分尺寸没有可读取的 measurement 属性，请工程方确认关键净宽、退距、道路/消防相关尺寸。")
    for risk in risks[:3]:
        lines.append(f"- 读图疑点：{risk}")
    lines.append("- 施工/审查影响：请工程方说明哪些内容已审定、哪些只是方案或待复核信息。")
    return lines


def consulting_lines(summary: dict[str, Any], risks: list[str]) -> list[str]:
    lines = [
        "我先按工程咨询角度做第一遍读图：",
        f"- 图纸识别：{summary_identity(summary)}。",
        f"- 已读到的基础信息：普通文字 {summary.get('text_count', 0)} 个，普通尺寸 {summary.get('dimension_count', 0)} 个，图层 {summary.get('layer_count', 0)} 个。",
    ]
    if summary.get("entity_counts"):
        top = list((summary.get("entity_counts") or {}).items())[:5]
        lines.append("- 主要对象类型：" + "、".join(f"{k}:{v}" for k, v in top))
    if risks:
        lines.append("- 需要优先复核：" + "；".join(risks[:4]))
    else:
        lines.append("- 在已读取范围内暂未发现明显读图风险，但这不等同于设计/审查通过。")
    lines.append("- 下一步建议：围绕关键区域、尺寸、轴网/房间/构件编号继续局部放大核对。")
    return lines


def usability_lines(summary: dict[str, Any], risks: list[str]) -> list[str]:
    blockers: list[str] = []
    if summary.get("proxy_count", 0):
        blockers.append("存在天正/T20或其他代理对象，关键内容可能未完全展开")
    if summary.get("units") in (None, "", "Unitless") or not summary.get("ins_units"):
        blockers.append("单位/比例信息不够明确")
    if risks:
        blockers.extend(risks[:2])
    if blockers:
        tier = "不能直接作为施工、审查或算量定稿依据"
    elif summary.get("dimension_count", 0) and summary.get("text_count", 0):
        tier = "可以作为初步读图和会议讨论依据，但关键尺寸仍需人工抽查"
    else:
        tier = "只能作为弱证据预览，不能直接使用"
    lines = [tier + "。"]
    if blockers:
        lines.append("主要原因：" + "；".join(dict.fromkeys(blockers)) + "。")
    lines.append("建议把结论分三档处理：可确认的普通文字/尺寸、可初步判断的图面关系、必须人工确认的代理对象/单位/关键尺寸。")
    return lines


def ask_answer(question: str, summary: dict[str, Any], matches: list[dict[str, Any]]) -> str:
    risks = list(summary.get("risks") or [])
    fallback = summary.get("proxy_fallback") or {}
    intent = ask_intent(question)
    risk_question = intent == "risk" or bool(re.search(r"不对劲|问题|风险|审查|检查|有没有|明显", question))
    lines: list[str] = []
    if intent == "meeting":
        lines.extend(meeting_question_lines(summary, risks))
    elif intent == "consulting":
        lines.extend(consulting_lines(summary, risks))
    elif intent == "usability":
        lines.extend(usability_lines(summary, risks))
    elif risk_question:
        if risks:
            lines.append("从当前可读取的 CAD 证据看，最需要注意的是：")
            for risk in risks[:5]:
                lines.append(f"- {risk}")
        else:
            lines.append("当前可读取的 CAD 证据里，没有发现明显的读图风险；关键尺寸和比例仍建议按出图标准复核。")
    elif matches:
        lines.append(f"我找到了 {len(matches)} 个和问题相关的 CAD 对象，已生成定位证据和局部范围。")
    else:
        lines.append("我已完成图纸读取和初步检查，但没有从文字、尺寸、图层或块名中定位到明确匹配项。")

    if isinstance(fallback, dict) and fallback.get("active"):
        lines.append("这张图含天正/T20 代理对象；普通 CAD 内容可以继续分析，代理对象内部内容需要转换版 DWG/PDF 才能作为可靠结论。")
    if summary.get("dimension_count"):
        lines.append(f"已读取普通尺寸 {summary.get('dimension_count')} 个，其中 {summary.get('dimension_measurement_count', 0)} 个带 measurement。")
    if summary.get("text_count"):
        lines.append(f"已读取普通文字 {summary.get('text_count')} 个。")
    return "\n".join(lines)


def write_tangent_conversion_request(work_root: Path, drawing: Path, summary: dict[str, Any]) -> Path | None:
    fallback = summary.get("proxy_fallback") or {}
    if not isinstance(fallback, dict) or not fallback.get("conversion_required"):
        return None
    type_hints = summary.get("proxy_type_hints") or {}
    layers = summary.get("proxy_layers") or {}
    type_lines = "\n".join(f"- {name}: {count}" for name, count in list(type_hints.items())[:20]) or "- 未识别到具体 TDb 类型"
    layer_lines = "\n".join(f"- {name}: {count}" for name, count in list(layers.items())[:20]) or "- 未识别到代理对象图层"
    text = f"""# 天正/T20 代理对象转换要求

图纸文件：`{drawing}`

当前自动读图发现该 DWG 含有天正/T20 自定义代理对象。Linux BricsCAD 只能读取代理外壳和类型线索，不能稳定读取代理对象内部文字、尺寸、符号、坐标和几何边界。请使用安装了匹配版本天正/T20 插件的 CAD 环境重新导出。

## 请提供

1. 普通 AutoCAD 对象版 DWG：将天正/T20 自定义对象转换、分解或导出为普通 `TEXT/MTEXT/DIMENSION/INSERT/LWPOLYLINE/LINE` 等对象。
2. 同版 PDF：用于视觉复核文字、尺寸和符号。
3. 如可选，请同时提供原始 DWG，便于对比转换前后差异。

## 转换要求

- 保持原图坐标、比例、单位和图层名称。
- 不要只截图或只导出图片。
- 不要压扁成单一块，尽量保留可选中、可查询的文字、尺寸、块和线对象。
- 转换后请打开文件确认不再出现大量 `ACAD_PROXY_ENTITY` 或天正代理对象提示。

## 本次检测到的代理类型

{type_lines}

## 代理对象主要图层

{layer_lines}

## 当前可继续使用的内容

- 普通 CAD 实体、普通文字、普通尺寸、图层、基础几何量测。
- 代理对象只能作为“需要转换/复核”的证据，不能作为最终工程量依据。
"""
    out = output_root(work_root, drawing) / "天正T20代理对象转换要求.md"
    out.write_text(text, encoding="utf-8")
    return out


def command_health(args: argparse.Namespace) -> int:
    work_root = Path(args.work_root).resolve()
    load_env_files(work_root)
    status, response = remote_request("/health", timeout=remote_timeout(30))
    bricscad_ok = status == 200 and response.get("ok") is True
    tangent = tangent_worker_health()
    print_json({
        "ok": bricscad_ok,
        "service": "remote-bricscad-reader",
        "config_source": remote_config_source(),
        "status": status,
        "endpoint_configured": bool(remote_url()),
        "response": response,
        "tangent_worker": tangent,
    })
    return 0 if bricscad_ok else 1


def command_inspect(args: argparse.Namespace) -> int:
    work_root = Path(args.work_root).resolve()
    load_env_files(work_root)
    drawing = Path(args.file).resolve()
    payload, summary = ensure_extracted(work_root, drawing, args.entity_limit, args.force, args.preview)
    if not payload:
        print_json({"ok": False, "tool": "cad.inspect", **(summary or {})})
        return 2
    summary = summary or {}
    summary["summary_path"] = str(extraction_paths(work_root, drawing)["summary"])
    print_json({"ok": True, "tool": "cad.inspect", "data": summary})
    return 0


def command_render(args: argparse.Namespace) -> int:
    work_root = Path(args.work_root).resolve()
    load_env_files(work_root)
    drawing = Path(args.file).resolve()
    payload, summary = ensure_extracted(work_root, drawing, args.entity_limit, args.force, False)
    if not payload:
        print_json({"ok": False, "tool": "cad.render", **(summary or {})})
        return 2
    bbox = resolve_followup_bbox(args, summary) or (summary or {}).get("extents")
    if not bbox:
        print_json({"ok": False, "tool": "cad.render", "error": "no bbox available; run inspect and verify extents"})
        return 2
    if args.output:
        name = Path(args.output).stem
    else:
        name = "region" if args.bbox or getattr(args, "previous_bbox", None) else "full"
    preview, render_mode, render_error = render_bbox_image(work_root, drawing, payload, bbox, name)
    packet = evidence_packet(
        drawing,
        {"type": "render", "bbox": bbox, "focus_id": getattr(args, "focus_id", ""), "parent_evidence_id": getattr(args, "evidence_id", "")},
        summary,
        bbox=bbox,
        preview=preview,
        confidence=0.9 if preview else 0.2,
        focus_id=getattr(args, "focus_id", "") or focus_id_for(bbox, "render"),
        parent_evidence_id=getattr(args, "evidence_id", ""),
        render_mode=render_mode,
        evidence_strength="strong" if preview else "weak",
        render_error=render_error if not preview else {},
    )
    packet_path = output_root(work_root, drawing) / safe_filename(f"{packet['focus_id']}_render_evidence", ".json")
    write_json(packet_path, packet)
    if not preview:
        print_json({
            "ok": False,
            "tool": "cad.render",
            "error": "remote BricsCAD render failed; no local raster/vector fallback was used",
            "render_error": render_error,
            "evidence": str(packet_path),
            "bbox": bbox,
            "focus_id": packet["focus_id"],
            "evidence_id": packet["evidence_id"],
            "render_mode": render_mode,
            "evidence_strength": "weak",
        })
        return 2
    print_json({
        "ok": True,
        "tool": "cad.render",
        "preview_image": str(preview),
        "evidence": str(packet_path),
        "bbox": bbox,
        "focus_id": packet["focus_id"],
        "evidence_id": packet["evidence_id"],
        "render_mode": render_mode,
        "evidence_strength": "strong",
    })
    return 0


def filtered_items(payload: dict[str, Any], bbox: list[float] | None = None, types: set[str] | None = None, layers: set[str] | None = None) -> list[dict[str, Any]]:
    result = []
    for item in list_items(payload, "entities"):
        object_name = str(item.get("object_name") or "").upper()
        layer = str(item.get("layer") or "")
        if types and object_name not in types:
            continue
        if layers and layer not in layers:
            continue
        if in_bbox(item, bbox):
            result.append(item)
    return result


def command_region(args: argparse.Namespace) -> int:
    work_root = Path(args.work_root).resolve()
    load_env_files(work_root)
    drawing = Path(args.file).resolve()
    payload, summary = ensure_extracted(work_root, drawing, args.entity_limit, args.force, False)
    if not payload:
        print_json({"ok": False, "tool": "cad.region", **(summary or {})})
        return 2
    bbox = resolve_followup_bbox(args, summary)
    if not bbox:
        print_json({"ok": False, "tool": "cad.region", "error": "region requires --bbox, --previous-bbox, or a focus bbox from the native adapter"})
        return 2
    types = {part.upper() for part in arg_list(args.types)} or None
    layers = set(arg_list(args.layers)) or None
    items = [compact_item(item) for item in filtered_items(payload, bbox, types, layers)[: args.limit]]
    preview = None
    render_mode = ""
    render_error: dict[str, Any] = {}
    if args.render:
        preview, render_mode, render_error = render_bbox_image(work_root, drawing, payload, bbox, f"region_{bbox[0]:.0f}_{bbox[1]:.0f}_{bbox[2]:.0f}_{bbox[3]:.0f}")
    focus_id = getattr(args, "focus_id", "") or focus_id_for(bbox, "region")
    packet = evidence_packet(
        drawing,
        {"type": "region", "bbox": bbox, "types": arg_list(args.types), "layers": arg_list(args.layers)},
        summary,
        bbox=bbox,
        preview=preview,
        items=items,
        confidence=0.88 if preview else 0.66,
        focus_id=focus_id,
        parent_evidence_id=getattr(args, "evidence_id", ""),
        render_mode=render_mode,
        evidence_strength="strong" if preview and items else "moderate" if items else "weak",
        render_error=render_error if args.render and not preview else {},
    )
    packet_path = output_root(work_root, drawing) / safe_filename(f"{focus_id}_region_evidence", ".json")
    write_json(packet_path, packet)
    print_json({
        "ok": True,
        "tool": "cad.region",
        "bbox": bbox,
        "focus_id": packet["focus_id"],
        "evidence_id": packet["evidence_id"],
        "item_count": len(items),
        "preview_image": str(preview) if preview else "",
        "evidence": str(packet_path),
        "render_mode": render_mode,
        "render_error": render_error if args.render and not preview else {},
        "evidence_strength": packet["evidence_strength"],
        "items": items,
    })
    return 0


def command_query(args: argparse.Namespace) -> int:
    work_root = Path(args.work_root).resolve()
    load_env_files(work_root)
    drawing = Path(args.file).resolve()
    payload, summary = ensure_extracted(work_root, drawing, args.entity_limit, args.force, False)
    if not payload:
        print_json({"ok": False, "tool": "cad.query", **(summary or {})})
        return 2
    flags = 0 if args.case_sensitive else re.I
    pattern = re.compile(args.pattern if args.regex else re.escape(args.pattern), flags)
    bbox = resolve_followup_bbox(args, summary)
    matches = []
    for item in list_items(payload, "entities"):
        blob = " ".join([*item_text_values(item), str(item.get("object_name") or "")])
        if pattern.search(blob) and in_bbox(item, bbox):
            matches.append(compact_item(item))
            if len(matches) >= args.limit:
                break
    combined_bbox = None
    for item in matches:
        combined_bbox = merge_bbox(combined_bbox, item.get("bbox"))
    view_bbox = pad_bbox(combined_bbox) if combined_bbox else bbox
    focus_id = getattr(args, "focus_id", "") or focus_id_for(view_bbox, "query")
    packet = evidence_packet(
        drawing,
        {"type": "query", "pattern": args.pattern, "bbox": bbox},
        summary,
        bbox=view_bbox,
        items=matches,
        confidence=0.84,
        focus_id=focus_id,
        parent_evidence_id=getattr(args, "evidence_id", ""),
        evidence_strength="moderate" if matches else "weak",
    )
    packet_path = output_root(work_root, drawing) / safe_filename(f"{focus_id}_query_{args.pattern}", ".json")
    write_json(packet_path, packet)
    print_json({"ok": True, "tool": "cad.query", "match_count": len(matches), "focus_bbox": view_bbox, "focus_id": packet["focus_id"], "evidence_id": packet["evidence_id"], "evidence": str(packet_path), "matches": matches})
    return 0


def command_measure(args: argparse.Namespace) -> int:
    work_root = Path(args.work_root).resolve()
    load_env_files(work_root)
    drawing = Path(args.file).resolve()
    payload, summary = ensure_extracted(work_root, drawing, args.entity_limit, args.force, False)
    if not payload:
        print_json({"ok": False, "tool": "cad.measure", **(summary or {})})
        return 2
    bbox = resolve_followup_bbox(args, summary)
    types = {part.upper() for part in arg_list(args.types)} or None
    layers = set(arg_list(args.layers)) or None
    if args.points:
        p = [float(v) for v in args.points]
        result = {"mode": "point_distance", "distance": distance([p[0], p[1]], [p[2], p[3]]), "points": p}
        bbox_points = normalize_bbox([p[0], p[1], p[2], p[3]])
        focus_id = getattr(args, "focus_id", "") or focus_id_for(bbox_points, "measure")
        packet = evidence_packet(
            drawing,
            {"type": "measure", "points": p},
            summary,
            bbox=bbox_points,
            confidence=0.7,
            focus_id=focus_id,
            parent_evidence_id=getattr(args, "evidence_id", ""),
            evidence_strength="moderate",
        )
        packet_path = output_root(work_root, drawing) / safe_filename(f"{focus_id}_measure_points_evidence", ".json")
        write_json(packet_path, packet)
        print_json({"ok": True, "tool": "cad.measure", "focus_id": packet["focus_id"], "evidence_id": packet["evidence_id"], "data": result, "evidence": str(packet_path)})
        return 0
    raw_items = filtered_items(payload, bbox, types, layers)
    counts = Counter(str(item.get("object_name") or "UNKNOWN") for item in raw_items)
    block_counts = Counter(str(item.get("block_name") or "(anonymous)") for item in raw_items if str(item.get("object_name") or "").upper() == "INSERT")
    length_sum = 0.0
    area_sum = 0.0
    for item in raw_items:
        length, area = inferred_length_area(item)
        length_sum += length
        area_sum += area
    items = [compact_item(item) for item in raw_items[: args.limit]]
    focus_id = getattr(args, "focus_id", "") or focus_id_for(bbox, "measure")
    packet = evidence_packet(
        drawing,
        {"type": "measure", "bbox": bbox, "types": arg_list(args.types), "layers": arg_list(args.layers)},
        summary,
        bbox=bbox,
        items=items,
        confidence=0.78,
        focus_id=focus_id,
        parent_evidence_id=getattr(args, "evidence_id", ""),
        evidence_strength="moderate" if raw_items else "weak",
    )
    packet_path = output_root(work_root, drawing) / safe_filename(f"{focus_id}_measure_evidence", ".json")
    write_json(packet_path, packet)
    print_json({
        "ok": True,
        "tool": "cad.measure",
        "focus_id": packet["focus_id"],
        "evidence_id": packet["evidence_id"],
        "data": {
            "bbox": bbox,
            "object_count": len(raw_items),
            "object_counts": dict(counts),
            "block_counts": dict(block_counts.most_common(80)),
            "length_sum": length_sum,
            "area_sum": area_sum,
            "unit_note": "Uses drawing units from CAD payload; confirm plotted scale before treating as construction quantity.",
        },
        "evidence": str(packet_path),
        "sample_items": items,
    })
    return 0


def command_diagnose(args: argparse.Namespace) -> int:
    work_root = Path(args.work_root).resolve()
    load_env_files(work_root)
    drawing = Path(args.file).resolve()
    payload, summary = ensure_extracted(work_root, drawing, args.entity_limit, args.force, False)
    if not payload:
        print_json({"ok": False, "tool": "cad.diagnose", **(summary or {})})
        return 2
    summary = summary or {}
    risks = list(summary.get("risks") or [])
    if summary.get("layer_count", 0) > 120:
        risks.append("图层数量较多，建议按专业/图层缩小范围后复核")
    if summary.get("entity_count", 0) > args.entity_limit * 0.95:
        risks.append("对象数接近提取上限，可能需要提高 entity_limit")
    if summary.get("table_count", 0) == 0 and any("表" in text for text in summary.get("title_candidates", [])):
        risks.append("图名疑似表格图，但未提取到 CAD TABLE，可能是普通文字线框表")
    if summary.get("dimension_count", 0) and summary.get("dimension_measurement_count", 0) < summary.get("dimension_count", 0):
        risks.append("部分尺寸标注缺少 measurement 属性，建议用标注文字与几何距离交叉复核")
    if summary.get("proxy_count", 0):
        top_layers = ", ".join(f"{k}:{v}" for k, v in list((summary.get("proxy_layers") or {}).items())[:5])
        if top_layers:
            risks.append(f"代理对象主要集中在图层 {top_layers}")
        families = summary.get("proxy_families") or []
        if families:
            family_names = ", ".join(str(item.get("vendor") or item.get("family")) for item in families if isinstance(item, dict))
            risks.append(f"代理对象来源判断：{family_names}；需云端 Windows CAD worker 安装对应插件后才能读取内部语义")
            tangent = summary.get("tangent_worker") or {}
            if isinstance(tangent, dict) and not tangent.get("ok"):
                risks.append("未检测到可用天正 worker；当前只返回代理对象类型线索，不能稳定展开为普通 CAD 对象")
        fallback = summary.get("proxy_fallback") or {}
        if isinstance(fallback, dict) and fallback.get("active"):
            risks.append("已启用无天正 worker 降级模式：普通 CAD 证据可用，TDb* 代理对象按需转换/复核处理")
    if summary.get("attribute_text_count", 0):
        risks.append(f"已读取 {summary.get('attribute_text_count')} 个块属性文字，可用于门窗编号/轴号/构件编号定位")
    risks = list(dict.fromkeys(risks))
    answer = "；".join(risks) if risks else "未发现明显 CAD 读取风险；仍建议对关键尺寸和比例做人工抽查。"
    focus_bbox = resolve_followup_bbox(args, summary) or summary.get("extents")
    preview = None
    render_mode = ""
    render_error: dict[str, Any] = {}
    if getattr(args, "render_focus", False) and focus_bbox:
        preview, render_mode, render_error = render_bbox_image(work_root, drawing, payload, focus_bbox, "diagnose_focus")
    focus_id = getattr(args, "focus_id", "") or focus_id_for(focus_bbox, "diagnose")
    packet = evidence_packet(
        drawing,
        {"type": "diagnose"},
        summary,
        bbox=focus_bbox,
        preview=preview,
        answer=answer,
        confidence=0.72,
        focus_id=focus_id,
        parent_evidence_id=getattr(args, "evidence_id", ""),
        render_mode=render_mode,
        evidence_strength="moderate" if risks or preview else "weak",
        render_error=render_error if getattr(args, "render_focus", False) and not preview else {},
    )
    packet_path = output_root(work_root, drawing) / safe_filename(f"{focus_id}_diagnose_evidence", ".json")
    write_json(packet_path, packet)
    conversion_request = write_tangent_conversion_request(work_root, drawing, summary)
    print_json({
        "ok": True,
        "tool": "cad.diagnose",
        "answer": answer,
        "risks": risks,
        "focus_id": packet["focus_id"],
        "focus_bbox": focus_bbox,
        "evidence_id": packet["evidence_id"],
        "preview_image": str(preview) if preview else "",
        "render_mode": render_mode,
        "render_error": render_error if getattr(args, "render_focus", False) and not preview else {},
        "evidence_strength": packet["evidence_strength"],
        "fallback": summary.get("proxy_fallback"),
        "conversion_request": str(conversion_request) if conversion_request else "",
        "evidence": str(packet_path),
        "summary": summary,
    })
    return 0


def command_ask(args: argparse.Namespace) -> int:
    work_root = Path(args.work_root).resolve()
    load_env_files(work_root)
    drawing = Path(args.file).resolve()
    payload, summary = ensure_extracted(work_root, drawing, args.entity_limit, args.force, False)
    if not payload:
        print_json({"ok": False, "tool": "cad.ask", **(summary or {})})
        return 2
    summary = summary or {}
    matches = find_question_matches(payload, args.question, args.limit)
    combined_bbox = None
    for item in matches:
        combined_bbox = merge_bbox(combined_bbox, item.get("bbox"))

    preview = None
    focus_images: list[dict[str, Any]] = []
    render_warnings: list[dict[str, Any]] = []
    if not args.no_render:
        if combined_bbox:
            focus_bbox = pad_bbox(combined_bbox)
            preview, render_mode, render_error = render_bbox_image(work_root, drawing, payload, focus_bbox, "ask_focus")
            if preview:
                focus_images.append({"kind": "question_match", "preview_image": str(preview), "bbox": focus_bbox, "render_mode": render_mode})
            else:
                render_warnings.append({"kind": "question_match", "bbox": focus_bbox, "render_mode": render_mode, "render_error": render_error})
        else:
            fallback = summary.get("proxy_fallback") or {}
            proxy_layers = summary.get("proxy_layers") or {}
            layer_stats = summary.get("layers") or {}
            if isinstance(fallback, dict) and fallback.get("active") and proxy_layers:
                top_layer = next(iter(proxy_layers.keys()))
                layer_bbox = (layer_stats.get(top_layer) or {}).get("bbox") if isinstance(layer_stats, dict) else None
                if layer_bbox:
                    focus_bbox = pad_bbox(layer_bbox)
                    preview, render_mode, render_error = render_bbox_image(work_root, drawing, payload, focus_bbox, f"ask_proxy_layer_{top_layer}")
                    if preview:
                        focus_images.append({"kind": "proxy_layer", "layer": top_layer, "preview_image": str(preview), "bbox": focus_bbox, "render_mode": render_mode})
                    else:
                        render_warnings.append({"kind": "proxy_layer", "layer": top_layer, "bbox": focus_bbox, "render_mode": render_mode, "render_error": render_error})
            if preview is None and getattr(args, "previous_bbox", None):
                focus_bbox = normalize_bbox(args.previous_bbox)
                preview, render_mode, render_error = render_bbox_image(work_root, drawing, payload, focus_bbox, "ask_previous_focus")
                if preview:
                    focus_images.append({"kind": "previous_focus", "preview_image": str(preview), "bbox": focus_bbox, "render_mode": render_mode})
                else:
                    render_warnings.append({"kind": "previous_focus", "bbox": focus_bbox, "render_mode": render_mode, "render_error": render_error})
            if preview is None and summary.get("extents"):
                preview, render_mode, render_error = render_bbox_image(work_root, drawing, payload, summary["extents"], "ask_overview")
                if preview:
                    focus_images.append({"kind": "overview", "preview_image": str(preview), "bbox": summary["extents"], "render_mode": render_mode})
                else:
                    render_warnings.append({"kind": "overview", "bbox": summary["extents"], "render_mode": render_mode, "render_error": render_error})

    answer = ask_answer(args.question, summary, matches)
    focus_bbox = focus_images[0].get("bbox") if focus_images else (normalize_bbox(args.previous_bbox) if getattr(args, "previous_bbox", None) else summary.get("extents"))
    focus_id = getattr(args, "focus_id", "") or focus_id_for(focus_bbox, "ask")
    packet = evidence_packet(
        drawing,
        {"type": "ask", "question": args.question},
        summary,
        bbox=focus_bbox,
        preview=preview,
        items=matches,
        answer=answer,
        confidence=0.74 if matches else 0.68,
        focus_id=focus_id,
        parent_evidence_id=getattr(args, "evidence_id", ""),
        render_mode=(focus_images[0].get("render_mode") if focus_images else ""),
        evidence_strength="moderate" if preview or matches else "weak",
        render_error=(render_warnings[0].get("render_error") if render_warnings else {}),
    )
    packet_path = output_root(work_root, drawing) / safe_filename(f"{focus_id}_ask_evidence", ".json")
    write_json(packet_path, packet)
    conversion_request = write_tangent_conversion_request(work_root, drawing, summary)
    print_json({
        "ok": True,
        "tool": "cad.ask",
        "question": args.question,
        "answer": answer,
        "preview_image": str(preview) if preview else "",
        "focus_images": focus_images,
        "render_warnings": render_warnings,
        "match_count": len(matches),
        "matches": matches[: args.limit],
        "focus_id": packet["focus_id"],
        "previous_bbox": packet["previous_bbox"],
        "evidence_id": packet["evidence_id"],
        "evidence_strength": packet["evidence_strength"],
        "fallback": summary.get("proxy_fallback"),
        "conversion_request": str(conversion_request) if conversion_request else "",
        "evidence": str(packet_path),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stable local Agent tool API for cloud CAD reading, rendering, querying, measuring, and evidence.")
    parser.add_argument("--work-root", default=str(Path(__file__).resolve().parents[1]))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="Check remote CAD service").set_defaults(func=command_health)

    def add_file(p: argparse.ArgumentParser) -> None:
        p.add_argument("--file", required=True, help="DWG/DXF/PDF drawing file")
        p.add_argument("--entity-limit", type=int, default=50000)
        p.add_argument("--force", action="store_true")

    def add_focus(p: argparse.ArgumentParser) -> None:
        p.add_argument("--focus-id", default="", help="Continue from a previous focus id supplied by the native Agent adapter")
        p.add_argument("--previous-bbox", nargs=4, type=float, help="Previous CAD bbox: min_x min_y max_x max_y")
        p.add_argument("--evidence-id", default="", help="Parent evidence id supplied by the native Agent adapter")

    inspect = sub.add_parser("inspect", help="L0 structured file read and drawing summary")
    add_file(inspect)
    inspect.add_argument("--preview", action="store_true")
    inspect.set_defaults(func=command_inspect)

    render = sub.add_parser("render", help="L1/L2 render full drawing or bbox region")
    add_file(render)
    add_focus(render)
    render.add_argument("--bbox", nargs=4, type=float)
    render.add_argument("--output")
    render.set_defaults(func=command_render)

    region = sub.add_parser("region", help="L2 inspect a local bbox and optionally render it")
    add_file(region)
    add_focus(region)
    region.add_argument("--bbox", nargs=4, type=float)
    region.add_argument("--types", nargs="*", default=[])
    region.add_argument("--layers", nargs="*", default=[])
    region.add_argument("--limit", type=int, default=200)
    region.add_argument("--render", action="store_true")
    region.set_defaults(func=command_region)

    query = sub.add_parser("query", help="L3 locate text/block/layer/object matches")
    add_file(query)
    add_focus(query)
    query.add_argument("--pattern", required=True)
    query.add_argument("--regex", action="store_true")
    query.add_argument("--case-sensitive", action="store_true")
    query.add_argument("--bbox", nargs=4, type=float)
    query.add_argument("--limit", type=int, default=100)
    query.set_defaults(func=command_query)

    measure = sub.add_parser("measure", help="L4 count objects and compute basic length/area evidence")
    add_file(measure)
    add_focus(measure)
    measure.add_argument("--bbox", nargs=4, type=float)
    measure.add_argument("--types", nargs="*", default=[])
    measure.add_argument("--layers", nargs="*", default=[])
    measure.add_argument("--points", nargs=4, type=float, help="x1 y1 x2 y2 point distance")
    measure.add_argument("--limit", type=int, default=200)
    measure.set_defaults(func=command_measure)

    diagnose = sub.add_parser("diagnose", help="L5 first-pass CAD reading risk diagnosis")
    add_file(diagnose)
    add_focus(diagnose)
    diagnose.add_argument("--render-focus", action="store_true")
    diagnose.set_defaults(func=command_diagnose)

    ask = sub.add_parser("ask", help="High-level natural-language drawing question entrypoint")
    add_file(ask)
    add_focus(ask)
    ask.add_argument("--question", required=True)
    ask.add_argument("--limit", type=int, default=20)
    ask.add_argument("--no-render", action="store_true")
    ask.set_defaults(func=command_ask)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print_json({
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        raise SystemExit(1)

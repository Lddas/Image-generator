#!/usr/bin/env python3
"""Small local Seedream image generator with no third-party dependencies."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import ssl
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
LEDGER = ROOT / ".cost-ledger.json"
DEFAULT_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
DEFAULT_MODEL = "dola-seedream-5-0-pro-260628"
MODEL_PRICES = {
    "seedream-5-0-lite-260128": {"output": 0.035},
    "dola-seedream-5-0-pro-260628": {"small": 0.045, "large": 0.09},
}


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        os.environ.setdefault(key.strip(), value)


# `env` is deliberately non-hidden for local convenience. `.env` remains a
# backward-compatible fallback for older clones.
load_env(ROOT / "env")
load_env(ROOT / ".env")
PORT = int(os.environ.get("PORT", "8080"))


def api_key() -> str:
    return os.environ.get("ARK_API_KEY") or os.environ.get("SEEDANCE_API_KEY") or ""


def redact(text: str) -> str:
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._-]+", r"\1***", text, flags=re.I)
    return re.sub(r"(ark-)[A-Za-z0-9]+", r"\1***", text, flags=re.I)


def post_json(url: str, body: dict, timeout: int = 360) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("Missing ARK_API_KEY or SEEDANCE_API_KEY in .env")
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        message = detail
        try:
            parsed = json.loads(detail)
            message = parsed.get("error", {}).get("message") or detail
        except (json.JSONDecodeError, AttributeError):
            pass
        raise RuntimeError(f"Seedream HTTP {exc.code}: {redact(message)[:500]}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Seedream network error: {exc.reason}") from None


def download_image(item: dict, index: int) -> dict:
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"seedream_{stamp}_{index + 1:02d}_{uuid.uuid4().hex[:6]}.png"
    path = OUTPUTS / name
    if item.get("b64_json"):
        path.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        request = urllib.request.Request(item["url"], headers={"User-Agent": "SeedreamLocal/1.0"})
        with urllib.request.urlopen(request, timeout=180) as response:
            path.write_bytes(response.read())
    else:
        raise RuntimeError("Seedream returned an item without url or b64_json")
    return {"name": name, "url": f"/outputs/{name}"}


def image_data_uri(name: str) -> str:
    safe_name = Path(name).name
    path = OUTPUTS / safe_name
    if not safe_name or not path.is_file():
        raise RuntimeError("Selected input image no longer exists")
    mime = mimetypes.guess_type(safe_name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def output_price(model: str, item: dict) -> float:
    pricing = MODEL_PRICES.get(model)
    if not pricing:
        return 0.0
    if "output" in pricing:
        return pricing["output"]
    match = re.fullmatch(r"(\d+)x(\d+)", str(item.get("size") or ""))
    pixels = int(match.group(1)) * int(match.group(2)) if match else 0
    return pricing["large"] if pixels > 2_360_000 else pricing["small"]


def read_ledger() -> dict:
    if not LEDGER.is_file():
        return {"entries": []}
    try:
        data = json.loads(LEDGER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"entries": []}
    except (OSError, json.JSONDecodeError):
        return {"entries": []}


def append_cost(model: str, items: list[dict], usage: dict) -> float:
    cost = round(sum(output_price(model, item) for item in items), 6)
    ledger = read_ledger()
    ledger.setdefault("entries", []).append({
        "created": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "generated_images": int(usage.get("generated_images") or len(items)),
        "cost_usd": cost,
    })
    LEDGER.write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf-8")
    return cost


def cost_summary() -> dict:
    entries = read_ledger().get("entries") or []
    return {
        "total_usd": round(sum(float(entry.get("cost_usd") or 0) for entry in entries), 6),
        "generated_images": sum(int(entry.get("generated_images") or 0) for entry in entries),
        "calls": len(entries),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/api/health":
            self._json(200, {"ok": True, "keyConfigured": bool(api_key())})
            return
        if self.path == "/api/images":
            OUTPUTS.mkdir(exist_ok=True)
            files = sorted(
                (path for path in OUTPUTS.iterdir() if path.is_file() and path.name != ".gitkeep"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            self._json(200, {"ok": True, "images": [{"name": path.name, "url": f"/outputs/{path.name}"} for path in files]})
            return
        if self.path == "/api/cost":
            self._json(200, {"ok": True, **cost_summary()})
            return
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_DELETE(self) -> None:
        prefix = "/api/images/"
        if not self.path.startswith(prefix):
            self._json(404, {"ok": False, "error": "Not found"})
            return
        name = Path(self.path[len(prefix):]).name
        path = OUTPUTS / name
        if not name or name == ".gitkeep" or not path.is_file():
            self._json(404, {"ok": False, "error": "Image not found"})
            return
        # Keep deletes recoverable instead of unlinking paid outputs permanently.
        trash = OUTPUTS / ".trash"
        trash.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        trashed = trash / f"{stamp}_{name}"
        path.replace(trashed)
        self._json(200, {"ok": True, "deleted": name, "recoverable_at": str(trashed)})

    def do_POST(self) -> None:
        if self.path != "/api/generate":
            self._json(404, {"ok": False, "error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request_data = json.loads(self.rfile.read(length) or b"{}")
            prompt = str(request_data.get("prompt") or "").strip()
            if not prompt:
                self._json(400, {"ok": False, "error": "Prompt is required"})
                return
            count = max(1, min(6, int(request_data.get("count") or 1)))
            model = str(request_data.get("model") or os.environ.get("SEEDREAM_MODEL") or DEFAULT_MODEL)
            size = str(request_data.get("size") or "2K").upper()
            # Current Seedream 5 endpoints reject the legacy 1K shorthand.
            if size == "1K":
                size = "2K"
            body = {
                "model": model,
                "prompt": prompt,
                "size": size,
                "response_format": "url",
                "output_format": "png",
                "watermark": False,
            }
            input_image = str(request_data.get("input_image") or "").strip()
            if input_image:
                body["image"] = image_data_uri(input_image)
            base = os.environ.get("ARK_BASE_URL", DEFAULT_BASE).rstrip("/")
            images = []
            usage: dict[str, int] = {}
            request_cost = 0.0
            # Pro rejects sequential_image_generation, so request each output
            # independently. This also makes the selected count deterministic.
            for request_index in range(count):
                payload = post_json(f"{base}/images/generations", body)
                items = payload.get("data") or []
                if not items:
                    raise RuntimeError("Seedream returned no images")
                for item in items:
                    images.append(download_image(item, len(images)))
                request_usage = payload.get("usage") or {}
                request_cost += append_cost(model, items, request_usage)
                for key, value in request_usage.items():
                    if isinstance(value, (int, float)):
                        usage[key] = usage.get(key, 0) + value
            self._json(200, {
                "ok": True,
                "images": images,
                "model": model,
                "usage": usage,
                "request_cost_usd": round(request_cost, 6),
                "cost": cost_summary(),
            })
        except Exception as exc:
            self._json(502, {"ok": False, "error": redact(str(exc))})


if __name__ == "__main__":
    OUTPUTS.mkdir(exist_ok=True)
    print(f"Seedream image generator: http://127.0.0.1:{PORT}")
    print(f"Outputs: {OUTPUTS}")
    print(f"API key: {'configured' if api_key() else 'MISSING'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

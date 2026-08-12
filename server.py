#!/usr/bin/env python3
"""Small local Seedream image generator with no third-party dependencies."""

from __future__ import annotations

import base64
import json
import mimetypes
import math
import os
import re
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
INPUTS = ROOT / "inputs"
VIDEOS = ROOT / "videos"
SPRITES = ROOT / "sprites"
SEGMENTS = ROOT / "segments"
LEDGER = ROOT / ".cost-ledger.json"
DEFAULT_BASE = "https://ark.ap-southeast.bytepluses.com/api/v3"
DEFAULT_MODEL = "dola-seedream-5-0-pro-260628"
DEFAULT_VIDEO_MODEL = "seedance-1-5-pro-251215"
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


def fal_key() -> str:
    return os.environ.get("PLAYSTUDIO_FAL_KEY") or os.environ.get("FAL_KEY") or os.environ.get("FAL_API_KEY") or ""


def redact(text: str) -> str:
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._-]+", r"\1***", text, flags=re.I)
    return re.sub(r"(ark-)[A-Za-z0-9]+", r"\1***", text, flags=re.I)


def post_json(url: str, body: dict, timeout: int = 360) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("Missing ARK_API_KEY or SEEDANCE_API_KEY in env")
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
        raise RuntimeError(f"ModelArk HTTP {exc.code}: {redact(message)[:500]}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ModelArk network error: {exc.reason}") from None


def get_json(url: str, timeout: int = 60) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("Missing ARK_API_KEY or SEEDANCE_API_KEY in env")
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"Seedance HTTP {exc.code}: {redact(detail)[:500]}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Seedance network error: {exc.reason}") from None


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
    path = selected_image_path(name)
    safe_name = path.name
    mime = mimetypes.guess_type(safe_name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def selected_image_path(name: str) -> Path:
    requested = Path(name)
    if requested.parts[:1] == ("inputs",):
        folder = INPUTS
    elif requested.parts[:1] == ("segments",):
        folder = SEGMENTS
    else:
        folder = OUTPUTS
    path = folder / requested.name
    if not requested.name or not path.is_file():
        raise RuntimeError("Selected input image no longer exists")
    return path


def run_sam_segment(request_data: dict) -> dict:
    name = str(request_data.get("input_image") or "")
    source = selected_image_path(name)
    x = max(0.0, min(1.0, float(request_data.get("x", 0.5))))
    y = max(0.0, min(1.0, float(request_data.get("y", 0.5))))
    box = request_data.get("box") if isinstance(request_data.get("box"), dict) else None
    tampon = max(0, min(64, int(request_data.get("tampon") or 0)))
    key = fal_key()
    if not key:
        raise RuntimeError("fal.ai key missing — set FAL_KEY in env")
    SEGMENTS.mkdir(parents=True, exist_ok=True)
    output_name = f"sam_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}.png"
    destination = SEGMENTS / output_name
    mask_name = f"{Path(output_name).stem}_mask.png"
    saved_mask = SEGMENTS / mask_name
    dimensions = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(source)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    width, height = [int(value) for value in dimensions.split("x")]
    body = {"image_url": image_data_uri(name)}
    if box:
        x1 = round(max(0.0, min(1.0, float(box.get("x1", 0)))) * width)
        y1 = round(max(0.0, min(1.0, float(box.get("y1", 0)))) * height)
        x2 = round(max(0.0, min(1.0, float(box.get("x2", 1)))) * width)
        y2 = round(max(0.0, min(1.0, float(box.get("y2", 1)))) * height)
        body["box_prompts"] = [{"x_min": min(x1, x2), "y_min": min(y1, y2), "x_max": max(x1, x2), "y_max": max(y1, y2)}]
    else:
        body["prompts"] = [{"x": round(x * width), "y": round(y * height), "label": 1}]
    request = urllib.request.Request(
        "https://fal.run/fal-ai/sam2/image",
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Key {key}", "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120, context=ssl.create_default_context()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise RuntimeError(f"fal.ai SAM2 HTTP {exc.code}: {redact(detail)[:500]}") from None
    masks = payload.get("masks") or []
    mask_url = ((masks[0] or {}).get("url") if masks else "") or ((payload.get("image") or {}).get("url") or "")
    if not mask_url:
        raise RuntimeError("fal.ai SAM2 returned no mask")
    with tempfile.TemporaryDirectory(prefix="fal-sam-") as tmp:
        mask_path = Path(tmp) / "mask.png"
        with urllib.request.urlopen(urllib.request.Request(mask_url, headers={"User-Agent": "SeedreamLocal/1.0"}), timeout=120) as response:
            mask_path.write_bytes(response.read())
        saved_mask.write_bytes(mask_path.read_bytes())
        alpha_filter = f"format=gray,scale={width}:{height}"
        if tampon:
            alpha_filter += "," + ",".join(["dilation"] * tampon)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(source), "-i", str(mask_path), "-filter_complex", f"[1:v]{alpha_filter}[a];[0:v][a]alphamerge", "-frames:v", "1", str(destination)],
            check=True, capture_output=True,
        )
    return {
        "name": f"segments/{output_name}", "display_name": output_name,
        "url": f"/segments/{output_name}", "mask_url": f"/segments/{mask_name}",
        "source_name": name, "source_url": f"/{'inputs' if Path(name).parts[:1] == ('inputs',) else 'outputs'}/{Path(name).name}",
        "tampon": tampon, "selection_mode": "box" if box else "point", "provider": "fal.ai SAM2", "width": width, "height": height,
    }


def detect_elements(name: str, max_parts: int = 16) -> tuple[list[dict], str]:
    model = os.environ.get("ARK_VISION_MODEL", "dola-seed-2-1-turbo-260628")
    instruction = (
        f"Find up to {max_parts} distinct reusable visual elements. Return ONLY a JSON array where each item is "
        "{\"label\":string,\"bbox\":{\"x1\":integer,\"y1\":integer,\"x2\":integer,\"y2\":integer}}. "
        "Coordinates are 0..999. Include characters, props and meaningful background elements; do not duplicate elements."
    )
    payload = post_json(
        f"{os.environ.get('ARK_BASE_URL', DEFAULT_BASE).rstrip('/')}/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": image_data_uri(name)}},
            {"type": "text", "text": instruction},
        ]}], "temperature": 0.1, "max_tokens": 1800},
        timeout=120,
    )
    content = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    match = re.search(r"\[[\s\S]*\]", str(content))
    raw_parts = json.loads(match.group(0) if match else str(content))
    parts = []
    for item in raw_parts[:max_parts]:
        bbox = item.get("bbox") if isinstance(item, dict) else None
        if isinstance(bbox, dict) and all(key in bbox for key in ("x1", "y1", "x2", "y2")):
            parts.append({"label": str(item.get("label") or f"element {len(parts)+1}"), "bbox": bbox})
    if not parts:
        raise RuntimeError("The vision model found no separable elements")
    return parts, model


def crop_element(source: Path, bbox: dict, destination: Path) -> None:
    dimensions = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(source)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    width, height = [int(value) for value in dimensions.split("x")]
    x1 = max(0, min(width - 1, round(float(bbox["x1"]) / 999 * width)))
    y1 = max(0, min(height - 1, round(float(bbox["y1"]) / 999 * height)))
    x2 = max(x1 + 1, min(width, round(float(bbox["x2"]) / 999 * width)))
    y2 = max(y1 + 1, min(height, round(float(bbox["y2"]) / 999 * height)))
    subprocess.run(["ffmpeg", "-y", "-i", str(source), "-vf", f"crop={x2-x1}:{y2-y1}:{x1}:{y1}", "-frames:v", "1", str(destination)], check=True, capture_output=True)


def decompose_image(request_data: dict) -> dict:
    name = str(request_data.get("input_image") or "")
    source = selected_image_path(name)
    mode = str(request_data.get("mode") or "separate")
    base = os.environ.get("ARK_BASE_URL", DEFAULT_BASE).rstrip("/")
    if mode == "sheet":
        payload = post_json(f"{base}/images/generations", {
            "model": DEFAULT_MODEL,
            "prompt": "Identify every distinct element and place all of them separately on one pure white background in a neat grid. No overlap, no original scene background. Preserve each element's complete shape, art style and proportions.",
            "image": image_data_uri(name), "size": "2K", "response_format": "url", "output_format": "png", "watermark": False,
        })
        items = payload.get("data") or []
        images = [download_image(item, index) for index, item in enumerate(items)]
        append_cost(DEFAULT_MODEL, items, payload.get("usage") or {})
        return {"images": images, "parts": [], "model": DEFAULT_MODEL, "mode": "sheet", "cost": cost_summary()}
    parts, vision_model = detect_elements(name, max(2, min(20, int(request_data.get("max_parts") or 16))))
    images = []
    with tempfile.TemporaryDirectory(prefix="decompose-") as tmp:
        for index, part in enumerate(parts):
            crop = Path(tmp) / f"part-{index}.png"
            crop_element(source, part["bbox"], crop)
            crop_uri = "data:image/png;base64," + base64.b64encode(crop.read_bytes()).decode("ascii")
            payload = post_json(f"{base}/images/generations", {
                "model": DEFAULT_MODEL,
                "prompt": f"Extract only the {part['label']} as one complete isolated game asset on a transparent background. Reconstruct occluded edges naturally. Preserve exact style, colors and proportions. No other objects, text, ground, backdrop or framing.",
                "image": crop_uri, "size": "2K", "response_format": "url", "output_format": "png", "watermark": False,
            })
            items = payload.get("data") or []
            append_cost(DEFAULT_MODEL, items, payload.get("usage") or {})
            for item in items:
                asset = download_image(item, len(images)); asset["label"] = part["label"]; images.append(asset)
    return {"images": images, "parts": parts, "model": DEFAULT_MODEL, "vision_model": vision_model, "mode": "separate", "cost": cost_summary()}


def find_video_url(value) -> str:
    if isinstance(value, dict):
        for key in ("video_url", "url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith("http") and (key == "video_url" or ".mp4" in candidate.lower()):
                return candidate
        for nested in value.values():
            found = find_video_url(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = find_video_url(nested)
            if found:
                return found
    return ""


def download_video(url: str) -> dict:
    VIDEOS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"seedance_{stamp}_{uuid.uuid4().hex[:6]}.mp4"
    path = VIDEOS / name
    request = urllib.request.Request(url, headers={"User-Agent": "SeedanceLocal/1.0"})
    with urllib.request.urlopen(request, timeout=600) as response:
        path.write_bytes(response.read())
    return {"name": name, "url": f"/videos/{name}"}


def generate_video(request_data: dict) -> tuple[dict, str]:
    prompt = str(request_data.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("Prompt is required")
    model = str(request_data.get("model") or DEFAULT_VIDEO_MODEL)
    names = [str(name) for name in request_data.get("input_images") or [] if str(name).strip()]
    max_images = 2 if model.startswith("seedance-1-") else 10
    names = names[:max_images]
    content = [{"type": "text", "text": prompt[:2000]}]
    for index, name in enumerate(names):
        item = {"type": "image_url", "image_url": {"url": image_data_uri(name)}}
        if model.startswith("seedance-1-"):
            item["role"] = "first_frame" if index == 0 else "last_frame"
        else:
            item["role"] = "reference_image"
        content.append(item)
    body = {
        "model": model,
        "content": content,
        "duration": max(2, min(12, int(request_data.get("duration") or 5))),
        "ratio": str(request_data.get("ratio") or "16:9"),
        "resolution": str(request_data.get("resolution") or "720p"),
        "watermark": False,
    }
    base = os.environ.get("ARK_BASE_URL", DEFAULT_BASE).rstrip("/")
    tasks_url = f"{base}/contents/generations/tasks"
    created = post_json(tasks_url, body, timeout=90)
    task_id = str(created.get("id") or (created.get("data") or {}).get("id") or "")
    direct_url = find_video_url(created)
    if direct_url:
        return download_video(direct_url), task_id
    if not task_id:
        raise RuntimeError("Seedance accepted no task and returned no video")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        task = get_json(f"{tasks_url}/{urllib.parse.quote(task_id)}")
        status = str(task.get("status") or (task.get("data") or {}).get("status") or "").lower()
        video_url = find_video_url(task)
        if video_url:
            return download_video(video_url), task_id
        if status in {"failed", "cancelled", "canceled", "expired"}:
            error = task.get("error") or (task.get("data") or {}).get("error") or status
            raise RuntimeError(f"Seedance task {status}: {error}")
        time.sleep(5)
    raise RuntimeError(f"Seedance task {task_id} did not finish within 15 minutes")


def generate_sprite(request_data: dict) -> dict:
    name = Path(str(request_data.get("video") or "")).name
    video_path = VIDEOS / name
    if not name or not video_path.is_file():
        raise RuntimeError("Select a generated video first")
    fps = max(1, min(24, int(request_data.get("fps") or 12)))
    cell = max(32, min(512, int(request_data.get("cell_size") or 128)))
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(video_path)],
            check=True, capture_output=True, text=True,
        )
        duration = float(probe.stdout.strip())
    except FileNotFoundError:
        raise RuntimeError("ffmpeg/ffprobe is required. Install ffmpeg, then restart the app") from None
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise RuntimeError(f"Could not inspect video: {exc}") from None
    frames = max(1, min(100, math.ceil(duration * fps)))
    columns = math.ceil(math.sqrt(frames))
    rows = math.ceil(frames / columns)
    SPRITES.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", video_path.stem)[:60]
    output_name = f"{stem}_{fps}fps_{cell}px_{uuid.uuid4().hex[:6]}.png"
    output_path = SPRITES / output_name
    filters = [f"fps={fps}"]
    if bool(request_data.get("remove_green")):
        filters.append("chromakey=0x00FF00:0.18:0.08")
    filters.extend([
        f"scale={cell}:{cell}:force_original_aspect_ratio=decrease",
        f"pad={cell}:{cell}:(ow-iw)/2:(oh-ih)/2:color=0x00000000",
        f"tile={columns}x{rows}:nb_frames={frames}:padding=0:margin=0",
    ])
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-vf", ",".join(filters), "-frames:v", "1", str(output_path)],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is required. Install ffmpeg, then restart the app") from None
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Sprite export failed: {exc.stderr[-400:]}") from None
    metadata = {
        "image": output_name, "source_video": name, "fps": fps,
        "frame_count": frames, "frame_width": cell, "frame_height": cell,
        "columns": columns, "rows": rows,
    }
    (SPRITES / f"{output_path.stem}.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return {"name": output_name, "url": f"/sprites/{output_name}", "metadata": metadata}


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
            self._json(200, {"ok": True, "keyConfigured": bool(api_key()), "falConfigured": bool(fal_key())})
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
        if self.path == "/api/videos":
            VIDEOS.mkdir(exist_ok=True)
            files = sorted(
                (path for path in VIDEOS.iterdir() if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            self._json(200, {"ok": True, "videos": [{"name": path.name, "url": f"/videos/{path.name}"} for path in files]})
            return
        if self.path == "/api/sprites":
            SPRITES.mkdir(exist_ok=True)
            files = sorted(
                (path for path in SPRITES.glob("*.png") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            self._json(200, {"ok": True, "sprites": [{"name": path.name, "url": f"/sprites/{path.name}"} for path in files]})
            return
        if self.path == "/api/segments":
            SEGMENTS.mkdir(exist_ok=True)
            files = sorted((path for path in SEGMENTS.glob("*.png") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
            self._json(200, {"ok": True, "segments": [{"name": f"segments/{path.name}", "display_name": path.name, "url": f"/segments/{path.name}"} for path in files]})
            return
        if self.path == "/api/cost":
            self._json(200, {"ok": True, **cost_summary()})
            return
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_DELETE(self) -> None:
        image_prefix = "/api/images/"
        video_prefix = "/api/videos/"
        if self.path.startswith(image_prefix):
            prefix, folder = image_prefix, OUTPUTS
        elif self.path.startswith(video_prefix):
            prefix, folder = video_prefix, VIDEOS
        elif self.path.startswith("/api/sprites/"):
            prefix, folder = "/api/sprites/", SPRITES
        else:
            self._json(404, {"ok": False, "error": "Not found"})
            return
        name = Path(self.path[len(prefix):]).name
        path = folder / name
        if not name or name == ".gitkeep" or not path.is_file():
            self._json(404, {"ok": False, "error": "Image not found"})
            return
        # Keep deletes recoverable instead of unlinking paid outputs permanently.
        trash = folder / ".trash"
        trash.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        trashed = trash / f"{stamp}_{name}"
        path.replace(trashed)
        if folder == SPRITES:
            metadata = SPRITES / f"{Path(name).stem}.json"
            if metadata.is_file():
                metadata.replace(trash / f"{stamp}_{metadata.name}")
        self._json(200, {"ok": True, "deleted": name, "recoverable_at": str(trashed)})

    def do_POST(self) -> None:
        parsed_path = urllib.parse.urlsplit(self.path)
        if parsed_path.path == "/api/upload":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0:
                    self._json(400, {"ok": False, "error": "An image file is required"})
                    return
                if length > 25 * 1024 * 1024:
                    self._json(413, {"ok": False, "error": "Image must be 25 MB or smaller"})
                    return
                raw = self.rfile.read(length)
                query = urllib.parse.parse_qs(parsed_path.query)
                original = Path(query.get("name", ["upload.png"])[0]).name
                ext = Path(original).suffix.lower()
                if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
                    mime_ext = mimetypes.guess_extension(self.headers.get_content_type())
                    ext = mime_ext if mime_ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"} else ".png"
                stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(original).stem).strip("-")[:50] or "upload"
                INPUTS.mkdir(exist_ok=True)
                saved_name = f"{stem}_{uuid.uuid4().hex[:6]}{ext}"
                (INPUTS / saved_name).write_bytes(raw)
                self._json(200, {
                    "ok": True,
                    "image": {
                        "name": f"inputs/{saved_name}",
                        "display_name": original,
                        "url": f"/inputs/{saved_name}",
                    },
                })
            except (ValueError, OSError) as exc:
                self._json(400, {"ok": False, "error": f"Invalid upload: {exc}"})
            except Exception as exc:
                self._json(500, {"ok": False, "error": redact(str(exc))})
            return
        if parsed_path.path == "/api/video/generate":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request_data = json.loads(self.rfile.read(length) or b"{}")
                video, task_id = generate_video(request_data)
                self._json(200, {
                    "ok": True,
                    "video": video,
                    "model": str(request_data.get("model") or DEFAULT_VIDEO_MODEL),
                    "task_id": task_id,
                })
            except Exception as exc:
                self._json(502, {"ok": False, "error": redact(str(exc))})
            return
        if parsed_path.path == "/api/sprite/generate":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request_data = json.loads(self.rfile.read(length) or b"{}")
                self._json(200, {"ok": True, "sprite": generate_sprite(request_data)})
            except Exception as exc:
                self._json(502, {"ok": False, "error": redact(str(exc))})
            return
        if parsed_path.path == "/api/segment/click":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request_data = json.loads(self.rfile.read(length) or b"{}")
                self._json(200, {"ok": True, "segment": run_sam_segment(request_data)})
            except subprocess.TimeoutExpired:
                self._json(504, {"ok": False, "error": "SAM timed out"})
            except subprocess.CalledProcessError as exc:
                detail = exc.stderr or exc.stdout or b"SAM failed"
                if isinstance(detail, bytes):
                    detail = detail.decode("utf-8", errors="replace")
                detail = detail[-800:]
                self._json(502, {"ok": False, "error": redact(detail)})
            except Exception as exc:
                self._json(502, {"ok": False, "error": redact(str(exc))})
            return
        if parsed_path.path == "/api/decompose":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request_data = json.loads(self.rfile.read(length) or b"{}")
                self._json(200, {"ok": True, **decompose_image(request_data)})
            except Exception as exc:
                self._json(502, {"ok": False, "error": redact(str(exc))})
            return
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
            sam_cutout = str(request_data.get("sam_cutout") or "").strip()
            sam_mask = str(request_data.get("sam_mask") or "").strip()
            if sam_cutout and sam_mask and input_image:
                body["image"] = [image_data_uri(input_image), image_data_uri(sam_mask), image_data_uri(sam_cutout)]
                body["prompt"] = (
                    prompt + "\nEdit only the region indicated by the second reference image mask. "
                    "The third reference is the isolated selected object. Preserve every pixel outside "
                    "the selected region and blend the edited object naturally into the original image."
                )
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
    INPUTS.mkdir(exist_ok=True)
    VIDEOS.mkdir(exist_ok=True)
    SPRITES.mkdir(exist_ok=True)
    SEGMENTS.mkdir(exist_ok=True)
    print(f"Seedream + Seedance generator: http://127.0.0.1:{PORT}")
    print(f"Outputs: {OUTPUTS}")
    print(f"API key: {'configured' if api_key() else 'MISSING'}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

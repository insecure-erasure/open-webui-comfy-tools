"""
title: Generate Video
author: Insecure Erasure
description: Generate videos through ComfyUI (WAN2.1 / WAN2.2 image-to-video)
version: 3.2
"""

import asyncio
import html
import json
import logging
import random as _random
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from pydantic import BaseModel, Field

from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)

# =============================================================================
# Model version options for the dropdown valve
# =============================================================================
_MODEL_VERSION_OPTIONS = [
    {"value": "wan21", "label": "Wan 2.1"},
    {"value": "wan22", "label": "Wan 2.2"},
]

# =============================================================================
# Length guardrails — valid values are 4n+1 (WAN temporal VAE stride)
# =============================================================================
_MIN_FRAMES = 81
_MAX_FRAMES = 161
_FRAMES_OPTIONS = [
    {"value": "0", "label": "System default"},
] + [
    {"value": str(n), "label": str(n)}
    for n in range(81, 162, 4)
]

_USER_FRAMES_OPTIONS = [
    {"value": str(n), "label": str(n)}
    for n in range(81, 162, 4)
]


_STEPS_OPTIONS = [
    {"value": str(i), "label": str(i)}
    for i in range(4, 11)
]


def _snap_to_valid_frames(n: int) -> int:
    """Snap to nearest valid frame count (4n + 1, clamped to [_MIN_FRAMES, _MAX_FRAMES])."""
    n = max(_MIN_FRAMES, min(n, _MAX_FRAMES))
    # Nearest 4n+1
    snapped = ((n - 1) // 4) * 4 + 1
    # Check if rounding to next 4n+1 is closer
    if n - snapped > 2:
        snapped += 4
    return min(snapped, _MAX_FRAMES)


# =============================================================================
# Model family configurations
# =============================================================================
VIDEO_MODEL_CONFIGS = {
    "wan21": {
        "workflow_file": "generate_video.json",
        "diffusion_model": "Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v-nvfp4.safetensors",
        "sampler": "euler",
        "scheduler": "simple",
        "steps": 4,
        "cfg": 1.0,
        "model_sampling_shift": 5,
        "nag_scale": 11,
        "nag_alpha": 0.25,
        "nag_tau": 2.5,
    },
    "wan22": {
        "workflow_file": "generate_video_wan22.json",
        "high": {
            "diffusion_model": "Wan2.2-I2V-A14B-Moe-Distill-Lightx2v-high-nvfp4.safetensors",
            "sampler": "heun",
            "scheduler": "simple",
            "steps": 4,
            "cfg": 1.0,
            "start_at_step": 0,
            "end_at_step": 2,
            "add_noise": "enable",
            "return_with_leftover_noise": "enable",
            "model_sampling_shift": 5,
            "nag_scale": 11,
            "nag_alpha": 0.25,
            "nag_tau": 2.5,
        },
        "low": {
            "diffusion_model": "Wan2.2-I2V-A14B-Moe-Distill-Lightx2v-low-nvfp4.safetensors",
            "sampler": "euler",
            "scheduler": "simple",
            "steps": 4,
            "cfg": 1.0,
            "start_at_step": 2,
            "end_at_step": 10000,
            "add_noise": "disable",
            "return_with_leftover_noise": "disable",
            "model_sampling_shift": 5,
            "nag_scale": 11,
            "nag_alpha": 0.25,
            "nag_tau": 2.5,
        },
    },
}


# =============================================================================
# Workflow node resolver — finds nodes by _meta.title (must be unique)
# =============================================================================

def _resolve_node(workflow: dict, title: str) -> tuple[str, dict]:
    """
    Find a workflow node by its _meta.title.

    Returns (node_id, node_dict). Titles must be unique in the workflow.
    """
    for node_id, node in workflow.items():
        if node.get("_meta", {}).get("title") == title:
            return (node_id, node)
    raise KeyError(
        f"Node with title {title!r} not found in workflow. "
        "Available titles: "
        + ", ".join(
            n.get("_meta", {}).get("title", "(no title)")
            for n in workflow.values()
        )
    )


# =============================================================================
# Workflow loader — cache/tools/<tool_id>/filename.json
# =============================================================================

def _load_workflow(tool_id: str, filename: str) -> str:
    """
    Load the workflow JSON from the tool's cache directory.

    Resolves CACHE_DIR / 'tools' / <tool_id> / <filename>.
    Returns the raw JSON string, ready for json.loads() followed by
    _resolve_node().

    Raises RuntimeError if the tool_id is empty or the file is not found.
    """
    if not tool_id:
        raise RuntimeError(
            "No tool_id provided. The tool must run inside Open WebUI "
            "to resolve the workflow from cache."
        )

    from open_webui.config import CACHE_DIR

    workflow_path = CACHE_DIR / 'tools' / tool_id / filename

    if not workflow_path.exists():
        raise FileNotFoundError(
            f"Workflow file not found at {workflow_path}. "
            f"Copy {filename} from the tool's directory to that path."
        )

    log.info("Loading workflow from %s", workflow_path)
    return workflow_path.read_text(encoding='utf-8')


# =============================================================================
# ComfyUI constants
# =============================================================================
_COMFY_SEED_MAX: int = 1125899906842624

# =============================================================================
# ComfyUI API helpers
# =============================================================================

_COMFY_QUEUE_MAX_RETRIES = 600       # ~10 min at 1s intervals
_COMFY_QUEUE_POLL_INTERVAL = 1.0     # seconds


async def _comfyui_queue_prompt(
    client: httpx.AsyncClient, base_url: str, api_key: str, workflow: dict
) -> str:
    """Submit a workflow to ComfyUI and return the prompt_id."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "prompt": workflow,
        "client_id": str(uuid.uuid4()),
    }

    resp = await client.post(
        f"{base_url.rstrip('/')}/prompt",
        json=payload,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return a prompt_id: {data}")
    return prompt_id


async def _comfyui_wait_for_output(
    client: httpx.AsyncClient, base_url: str, api_key: str, prompt_id: str
) -> dict:
    """Poll /history/{prompt_id} until the workflow completes. Returns the output dict."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    history_url = f"{base_url.rstrip('/')}/history/{prompt_id}"

    for attempt in range(_COMFY_QUEUE_MAX_RETRIES):
        resp = await client.get(history_url, headers=headers, timeout=10)
        resp.raise_for_status()
        history = resp.json()

        if prompt_id in history and history[prompt_id].get("outputs"):
            return history[prompt_id]["outputs"]

        # Check if still in queue — the key might exist but have no outputs yet
        if prompt_id in history and history[prompt_id].get("status", {}).get("completed") is False:
            await asyncio.sleep(_COMFY_QUEUE_POLL_INTERVAL)
            continue

        # Key not in history yet → still queued/processing
        await asyncio.sleep(_COMFY_QUEUE_POLL_INTERVAL)

    raise TimeoutError(
        f"ComfyUI did not finish within {_COMFY_QUEUE_MAX_RETRIES} seconds "
        f"(prompt_id={prompt_id})"
    )


async def _comfyui_interrupt(base_url: str, api_key: str) -> None:
    """Interrupt the currently running ComfyUI generation."""
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{base_url.rstrip('/')}/interrupt",
                headers=headers,
                timeout=5,
            )
    except Exception:
        log.warning("Failed to interrupt ComfyUI", exc_info=True)


def _extract_video_filename(outputs: dict, output_node_id: str) -> tuple[str, str]:
    """
    Extract the video filename and type from the workflow outputs.

    Returns (filename, type). type is "output" or "temp" depending on
    whether the node saved to disk or only kept the result in memory.

    Tries common output keys used by video nodes:
      - "gifs" (VHS_VideoCombine)
      - "videos" (some custom nodes)
      - Falls back to "images"
    """
    node_output = outputs.get(output_node_id, {})

    for key in ("gifs", "videos", "images"):
        items = node_output.get(key, [])
        if items and isinstance(items, list) and len(items) > 0:
            filename = items[0].get("filename")
            img_type = items[0].get("type", "output")
            if filename:
                return (filename, img_type)

    raise RuntimeError(
        f"Could not find a video filename in output node {output_node_id}. "
        f"Available outputs: {json.dumps(node_output, indent=2)}"
    )


# =============================================================================
# LoRA helpers
# =============================================================================

def _parse_lora_config(raw: str, label: str) -> tuple[list, str | None]:
    """
    Parse a lora_config JSON string.

    Expected format: JSON array of strings or objects.
      - String: shorthand for {"model": <string>, "strength": 1.0}
      - Object: {"model": "...", "strength": 1.0, "path": "high"|"low"}
        - model (str, required): LoRA filename
        - strength (float, optional, default 1.0): LoRA strength.
          strength=0 disables the LoRA and frees the slot.
        - path (str, optional): "high" / "low". Omit for all paths.

    Returns (list, error_or_None). Strings are expanded to objects.
    """
    if not raw or raw.strip() == "" or raw.strip() == "[]":
        return [], None
    try:
        p = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return [], f"Invalid JSON in {label} lora_config: {e}"
    if not isinstance(p, list):
        return [], f"{label} lora_config must be a JSON array, got {type(p).__name__}"
    result = []
    for i, item in enumerate(p):
        if isinstance(item, str):
            # String shorthand → {"model": str, "strength": 1.0}
            result.append({"model": item, "strength": 1.0})
        elif isinstance(item, dict):
            if "model" not in item or not isinstance(item["model"], str):
                return [], f"{label} lora_config[{i}] must have a 'model' string field"
            strength = item.get("strength", None)
            if strength is not None and not isinstance(strength, (int, float)):
                return [], f"{label} lora_config[{i}] 'strength' must be a number, got {type(strength).__name__}"
            path_val = item.get("path", None)
            if path_val is not None and path_val not in ("high", "low"):
                return [], f"{label} lora_config[{i}] 'path' must be 'high', 'low', or omitted"
            result.append(item)
        else:
            return [], f"{label} lora_config[{i}] must be a string or object, got {type(item).__name__}"
    return result, None


async def _validate_loras_on_server(
    lora_list: list,
    comfy_base_url: str,
    api_key: str = "",
) -> list[str]:
    """
    Check that LoRA filenames exist on the ComfyUI server.

    Returns a list of missing filenames. An empty list means all were found
    or the server couldn't be reached.
    """
    if not lora_list:
        return []

    names_to_check = set()
    for item in lora_list:
        if isinstance(item, str):
            names_to_check.add(item.replace("\\", "/").rsplit("/", 1)[-1])
        elif isinstance(item, dict):
            name = item.get("model", "")
            if name:
                names_to_check.add(name.replace("\\", "/").rsplit("/", 1)[-1])
    if not names_to_check:
        return []

    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{comfy_base_url.rstrip('/')}/models/loras",
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            server_loras = resp.json()
            server_basenames = {
                p.replace("\\", "/").rsplit("/", 1)[-1] for p in server_loras
            }
            return [n for n in names_to_check if n not in server_basenames]
    except Exception:
        return []


def _filter_loras_for_path(lora_list: list, path_name: str) -> list:
    """
    Filter LoRAs applicable to a specific path/ram.

    Items without 'path' apply to all paths.
    Items with 'path' matching path_name apply to that path only.
    """
    return [
        item for item in lora_list
        if item.get("path", path_name) == path_name
    ]


# =============================================================================
# Diffusion model helpers
# =============================================================================

def _parse_diffusion_model_config(raw: str, label: str) -> tuple[list | dict | None, str | None]:
    """
    Parse a diffusion_model config JSON string.

    Can be either:
      - A single object: {"model": "filename.safetensors"}
      - An array of objects: [{"model": "...", "path": "high"}, ...]

    Returns (parsed, error_or_None).
    """
    if not raw or raw.strip() == "":
        return None, None
    try:
        p = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        return None, f"Invalid JSON in {label} diffusion_model: {e}"
    # Single object
    if isinstance(p, dict):
        if "model" not in p or not isinstance(p["model"], str):
            return None, f"{label} diffusion_model object must have a 'model' string field"
        return p, None
    # Array
    if isinstance(p, list):
        for i, item in enumerate(p):
            if not isinstance(item, dict):
                return None, f"{label} diffusion_model[{i}] must be an object, got {type(item).__name__}"
            if "model" not in item or not isinstance(item["model"], str):
                return None, f"{label} diffusion_model[{i}] must have a 'model' string field"
            path_val = item.get("path", None)
            if path_val is not None and path_val not in ("high", "low"):
                return None, f"{label} diffusion_model[{i}] 'path' must be 'high', 'low', or omitted"
        return p, None
    return None, f"{label} diffusion_model must be a JSON object or array"


def _resolve_diffusion_model_for_path(
    config: dict | list | None, path_name: str, default_model: str
) -> str:
    """
    Resolve the diffusion model filename for a specific path.

    - If config is None: return default_model
    - If config is a single object without 'path': return its model
    - If config is an array: find item with matching path, or fallback to default
    """
    if config is None:
        return default_model
    if isinstance(config, dict):
        return config.get("model", default_model)
    if isinstance(config, list):
        for item in config:
            if item.get("path") == path_name:
                return item.get("model", default_model)
        # No matching path item → use default
        return default_model
    return default_model


# =============================================================================
# TOOL
# =============================================================================

class Tools:
    """
    Generate Video - animate an image into a video (image-to-video).

    Use when the user requests to animate an image into a video. Pass the
    image reference via the `image` parameter — either a filename from a
    previous generation (e.g. "abc123.png") or a direct URL to an external
    image (e.g. "https://..."). The tool auto-detects which one it is.

    The generated video is displayed in the chat as a Rich UI embed (video
    player with native controls, autoplay muted loop). The result is
    terminal: no context is returned (bare HTMLResponse), so the LLM
    receives the middleware's generic message and should simply acknowledge
    that the video was generated and shown.

    prompt: Video description. Translate the user's request into English
        internally, then enrich with visual motion details without changing
        the subject or scene.
    image: Filename from a previous generation (e.g. "abc123.png"), or a
        direct URL to an external image to animate ("https://...").
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        model_version: str = Field(
            default="wan21",
            description="Video model version.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _MODEL_VERSION_OPTIONS,
                }
            },
        )
        diffusion_model: str = Field(
            default="",
            description='JSON with diffusion model(s). Single object for Wan 2.1: {"model": "..."}. Array for Wan 2.2: [{"model": "...", "path": "high"}, ...]. Leave empty to use the built-in defaults.',
        )
        lora_config: str = Field(
            default="[]",
            description='JSON array of LoRAs. String=only name (strength 1.0), object={"model": "...", "strength": 1.0, "path": "high"|"low"}. strength=0 disables the LoRA and frees the slot. Omit "path" for all ramas.',
        )
        length: str = Field(
            default="161",
            description="Maximum number of frames / video length. Acts as a ceiling for user values. Default is the real maximum (161). Must be 4n+1.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [o for o in _FRAMES_OPTIONS if o["value"] != "0"],
                }
            },
        )
        negative_prompt: str = Field(
            default="",
            description="Negative prompt. Leave empty to use the built-in default.",
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for video links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        model_version: str = Field(
            default="",
            description="Video model version. Overrides the admin valve.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _MODEL_VERSION_OPTIONS,
                }
            },
        )
        diffusion_model: str = Field(
            default="",
            description='JSON with diffusion model(s). Overrides the admin valve and built-in defaults.',
        )
        lora_config: str = Field(
            default="[]",
            description='JSON array of LoRAs. Overrides the admin valve.',
        )
        length: str = Field(
            default="81",
            description="Number of frames / video length. Default 81. Must be 4n+1.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _USER_FRAMES_OPTIONS,
                }
            },
        )
        negative_prompt: str = Field(
            default="",
            description="Your preferred negative prompt. Leave empty to use the admin valve or built-in default.",
        )
        seed: int = Field(
            default=-1,
            description="Seed. -1 = random, >=0 = fixed seed for reproducibility.",
        )
        steps: str = Field(
            default="4",
            description="Inference steps (4-10). Wan 2.1: any value. Wan 2.2: odd values are rounded up to the nearest even.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _STEPS_OPTIONS,
                }
            },
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Override the admin valve or COMFYUI_BASE_URL for video links.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    def _build_video_player(self, video_url: str) -> str:
        """
        Build the self-contained video player embed for a single video URL.

        The URL is HTML-escaped so query strings (e.g. &filename=...&type=...)
        cannot break the markup.

        Layout: the player fits the chat container width and its height is capped
        at 65% of the available screen height (screen.availHeight) — the sizing
        decision recorded in DESIGN.md §6 (2026-08-04): 65vh. `vh` units inside
        the sandboxed iframe are useless (they refer to the iframe box, ~150px),
        so the cap is expressed via the device screen, exactly like the image
        viewer. The video's aspect ratio is NOT known a priori (unlike
        smart_generate_image, which reserves reduced_w:reduced_h), so the embed
        waits for the `loadedmetadata` event (videoWidth/videoHeight) before
        sizing — never a made-up fallback ratio (DESIGN.md §10.4) — and reports
        the player's own height via reportHeight() so the iframe hugs the video
        (no empty frame on wide desktop screens).

        The player uses the native controls (play/seek/volume/fullscreen); there
        is no lightbox and no download button (maintainer decision, 2026-08-04).
        `muted` is kept so autoplay works (browsers block autoplay with sound).
        The native fullscreen of the <video> (via its controls) does NOT cause
        the chat scroll jump, so no saveScroll/restoreScroll is needed here
        (unlike the image lightbox, DESIGN.md §10.8).
        """
        src = html.escape(video_url, quote=True)
        return (
f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{
  color-scheme:light dark;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;overflow:hidden;margin:0;padding:0}}
body{{display:flex;align-items:center;justify-content:center;background:transparent}}
.player{{max-width:100%;overflow:hidden;border-radius:12px;background:#000}}
.player video{{display:block;width:100%;height:100%;object-fit:contain;border-radius:12px}}
</style>
</head>
<body>
<div class="player" id="player">
  <video id="video" src="{src}" autoplay muted loop playsinline controls preload="metadata"></video>
</div>
<script>
const player=document.getElementById('player'),video=document.getElementById('video');
function reportHeight(){{parent.postMessage({{type:'iframe:height',height:player.offsetHeight||document.documentElement.scrollHeight}},'*')}}
function fit(){{
  // The video's aspect ratio is not known a priori (unlike
  // smart_generate_image, which reserves reduced_w:reduced_h): wait for the
  // real dimensions before sizing — never fall back to a made-up ratio
  // (DESIGN.md §10.4). Until then, report the current height and let the
  // media events correct it.
  if(!(video.videoWidth>0&&video.videoHeight>0)){{reportHeight();return;}}
  const r=video.videoWidth/video.videoHeight;
  // Sizing decision (DESIGN.md §6, 2026-08-04): 65vh cap. vh/vw units are
  // useless inside the sandboxed iframe (§10.7), so the cap is 65% of the
  // available screen height (screen.availHeight); the width derives from
  // the container width + aspect ratio and the height never overflows the
  // available screen space.
  const maxH=(screen.availHeight||screen.height||0)*0.65;
  let w=document.documentElement.clientWidth;
  if(maxH>0){{const wByH=maxH*r;if(wByH>0&&wByH<w)w=wByH;}}
  player.style.width=w+'px';
  player.style.height=(w/r)+'px';
  reportHeight();
}}
video.addEventListener('loadedmetadata',fit);
video.addEventListener('loadeddata',fit);
video.addEventListener('canplay',fit);
window.addEventListener('load',fit);
addEventListener('resize',fit);
new ResizeObserver(fit).observe(document.body);
fit();
</script>
</body>
</html>
"""
        )

    async def generate_video(
        self,
        prompt: str,
        image: str,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __id__: str = "",
    ):
        """
        Animate an image into a video (image-to-video).

        The video is displayed in the chat as a Rich UI embed (see DESIGN.md
        §6): a self-contained player (autoplay muted loop playsinline
        controls, height capped at 65vh) sized after the video metadata
        loads. Terminal result — the bare HTMLResponse (no tuple) means the
        LLM receives the middleware's generic message and should simply
        acknowledge that the video was generated.

        :param prompt: Video description in English, enriched with motion details.
        :param image: Filename from a previous generation (e.g. "abc123.png") or a direct URL to an external image ("https://...").
        """
        if __request__ is None:
            log.error("generate_video called without request context")
            return "Error: The tool could not be initialized."

        try:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\U0001f3ac Generating video...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            from open_webui.routers.images import get_image_config

            image_config = await get_image_config()

            # =================================================================
            # Resolve valves: UserValves > AdminValves > built-in defaults
            # =================================================================
            user_valves = (__user__ or {}).get("valves", None)

            # Model version: UserValves > AdminValves > "wan21"
            resolved_version = (
                user_valves.model_version if user_valves and user_valves.model_version
                else self.valves.model_version or "wan21"
            )
            if resolved_version not in VIDEO_MODEL_CONFIGS:
                resolved_version = "wan21"
            version_cfg = VIDEO_MODEL_CONFIGS[resolved_version]

            # Detect architecture: dual (has "high") or single
            is_dual = "high" in version_cfg

            # Diffusion model(s): UserValves > AdminValves > version_cfg
            raw_diffusion = (
                user_valves.diffusion_model if user_valves and user_valves.diffusion_model
                else self.valves.diffusion_model or ""
            )
            parsed_diffusion, err = _parse_diffusion_model_config(raw_diffusion, "user" if user_valves and user_valves.diffusion_model else "admin")
            if err:
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "error",
                                "content": f"Diffusion model config error: {err}",
                            },
                        }
                    )
                return f"Error: {err}"

            # LoRA config: UserValves > AdminValves
            raw_lora = (
                user_valves.lora_config if user_valves and user_valves.lora_config
                else self.valves.lora_config or "[]"
            )
            parsed_loras, err = _parse_lora_config(raw_lora, "user" if user_valves and user_valves.lora_config else "admin")
            if err:
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "error",
                                "content": f"LoRA config error: {err}",
                            },
                        }
                    )
                return f"Error: {err}"

            # Filter out disabled LoRAs (strength == 0)
            parsed_loras = [item for item in parsed_loras if float(item.get("strength", 1.0)) != 0]

            # Validate LoRAs exist on the ComfyUI server
            missing = await _validate_loras_on_server(
                parsed_loras,
                image_config.COMFYUI_BASE_URL,
                image_config.COMFYUI_API_KEY or "",
            )
            if missing:
                msg = f"LoRA(s) not found on server: {', '.join(missing)}"
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "error",
                                "content": msg,
                            },
                        }
                    )
                return f"Error: {msg}"

            # Length: the user valve selects frames directly (default 81).
            # The admin valve is a hard ceiling (default 161). Both dropdowns
            # only offer valid 4n+1 values, so no snap is needed; legacy
            # values ("0" = use admin, "-1", unparseable) fall back to the
            # defaults.
            try:
                admin_raw = int(self.valves.length)
            except (TypeError, ValueError):
                admin_raw = _MAX_FRAMES
            if admin_raw <= 0:
                admin_raw = _MAX_FRAMES  # legacy "-1"/"0" → no real ceiling

            try:
                user_raw = int(user_valves.length) if user_valves and user_valves.length else 81
            except (TypeError, ValueError):
                user_raw = 81
            if user_raw <= 0:
                user_raw = 81  # legacy "0" (use admin) → default

            resolved_length = min(user_raw, admin_raw)
            clamped = resolved_length < user_raw  # cut by admin ceiling

            # Notify the user (toast) when the admin ceiling cuts their choice.
            if clamped:
                note = (
                    f"Video length clamped from {user_raw} to {resolved_length} frames "
                    "(system limit)."
                )
                log.warning(note)
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "warning",
                                "content": f"\u26a0\ufe0f {note}",
                            },
                        }
                    )

            # Negative prompt: UserValves > AdminValves > leave empty (= use version default)
            resolved_neg = (
                user_valves.negative_prompt if user_valves and user_valves.negative_prompt
                else self.valves.negative_prompt or ""
            )

            # Seed: UserValve. -1 = random, >=0 = fixed
            user_seed = int(user_valves.seed) if user_valves and user_valves.seed != -1 else -1
            seed_arg = _random.randint(0, _COMFY_SEED_MAX) if user_seed == -1 else min(user_seed, _COMFY_SEED_MAX)

            # Steps: UserValve dropdown (default 4)
            # Wan 2.1: free; Wan 2.2: round up to even
            resolved_steps = int(user_valves.steps) if user_valves and user_valves.steps else 4
            if is_dual and resolved_steps % 2 != 0:
                resolved_steps += 1
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "warning",
                                "content": f"\u26a0\ufe0f Steps must be even for Wan 2.2. Rounded up to {resolved_steps}.",
                            },
                        }
                    )

            # Warn if length falls outside the recommended range for the chosen steps
            # Center: 81 + (steps - 4) * 12. Range: ±9 frames, snapped to valid 4n+1.
            _center = 81 + (resolved_steps - 4) * 12
            _low = _snap_to_valid_frames(_center - 9)
            _high = _snap_to_valid_frames(_center + 9)
            if resolved_length < _low:
                log.warning(
                    "Steps (%d) may be excessive for length %d (recommended min: %d)",
                    resolved_steps, resolved_length, _low,
                )
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "warning",
                                "content": (
                                    f"\u26a0\ufe0f {resolved_steps} steps may be excessive for {resolved_length} frames. "
                                    f"Consider increasing length or reducing steps."
                                ),
                            },
                        }
                    )
            elif resolved_length > _high:
                log.warning(
                    "Steps (%d) may be insufficient for length %d (recommended max: %d)",
                    resolved_steps, resolved_length, _high,
                )
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "warning",
                                "content": (
                                    f"\u26a0\ufe0f {resolved_steps} steps may be insufficient for {resolved_length} frames. "
                                    f"Consider reducing length or increasing steps."
                                ),
                            },
                        }
                    )

            # Base URL: UserValves > AdminValves > COMFYUI_BASE_URL
            user_video_base_url = (
                user_valves.comfyui_image_base_url
                if user_valves and user_valves.comfyui_image_base_url
                else ""
            )
            resolved_video_base_url = (
                user_video_base_url
                or self.valves.comfyui_image_base_url
                or image_config.COMFYUI_BASE_URL
            )

            # =================================================================
            # Build the workflow: load from cache and parse
            # =================================================================
            raw_workflow = _load_workflow(__id__, version_cfg["workflow_file"])
            workflow = json.loads(raw_workflow)

            # =================================================================
            # Resolve common nodes (exist in both workflows)
            # =================================================================
            _, positive_prompt = _resolve_node(workflow, "Positive Prompt")
            _, negative_prompt_node = _resolve_node(workflow, "Negative Prompt")
            _, default_neg_node = _resolve_node(workflow, "Default Wan negative prompt")
            _, load_image = _resolve_node(workflow, "Load Image (URL/Path)")
            _, vae_loader = _resolve_node(workflow, "Load VAE")
            _, clip_loader = _resolve_node(workflow, "CLIPLoader (GGUF)")
            _, wan_img2vid = _resolve_node(workflow, "WanImageToVideo")
            _, easy_seed = _resolve_node(workflow, "EasySeed")
            _, conditioning_concat = _resolve_node(workflow, "Conditioning (Concat)")
            _, unsharpen = _resolve_node(workflow, "Unsharpen mask")
            _, image_blend = _resolve_node(workflow, "Image Blend")
            _, rtx_sr = _resolve_node(workflow, "RTX Video Super Resolution")
            _, frame_interp = _resolve_node(workflow, "Frame Interpolate")
            _, frame_interp_loader = _resolve_node(workflow, "Load Frame Interpolation Model")
            output_node_id, _ = _resolve_node(workflow, "Output MP4")

            # CLIP Vision nodes — may not exist in all workflows
            try:
                _, clip_vision_encode = _resolve_node(workflow, "CLIP Vision Encode")
            except KeyError:
                clip_vision_encode = None

            # =================================================================
            # Resolve path-specific nodes (single or dual)
            # =================================================================
            if is_dual:
                path_configs = {
                    "high": version_cfg["high"],
                    "low": version_cfg["low"],
                }
                path_nodes = {}
                for path_name in ("high", "low"):
                    suffix = f" {path_name.upper()}"
                    _, unet = _resolve_node(workflow, f"Load Diffusion Model{suffix}")
                    _, lora_n = _resolve_node(workflow, f"Power Lora Loader (rgthree){suffix}")
                    _, msampling = _resolve_node(workflow, f"ModelSamplingSD3{suffix}")
                    _, nag = _resolve_node(workflow, f"NAG {path_name.upper()}")
                    _, ksampler = _resolve_node(workflow, f"KSampler {path_name.upper()}")
                    path_nodes[path_name] = {
                        "unet": unet,
                        "lora": lora_n,
                        "msampling": msampling,
                        "nag": nag,
                        "ksampler": ksampler,
                    }
            else:
                path_configs = {"main": version_cfg}
                _, unet = _resolve_node(workflow, "Load Diffusion Model")
                _, lora_n = _resolve_node(workflow, "Power Lora Loader (rgthree)")
                _, msampling = _resolve_node(workflow, "ModelSamplingSD3")
                _, nag = _resolve_node(workflow, "NAG HIGH")
                _, ksampler = _resolve_node(workflow, "KSampler")
                path_nodes = {
                    "main": {
                        "unet": unet,
                        "lora": lora_n,
                        "msampling": msampling,
                        "nag": nag,
                        "ksampler": ksampler,
                    }
                }

            # =================================================================
            # Inject common values
            # =================================================================
            positive_prompt["inputs"]["text"] = prompt
            easy_seed["inputs"]["seed"] = seed_arg

            # Configure image source
            node_img = load_image["inputs"]
            parsed = urlparse(image)
            if parsed.scheme and parsed.netloc:
                node_img["source"] = "url"
                node_img["url"] = image
                node_img.pop("image", None)
                node_img.pop("Choose file to upload", None)
            else:
                node_img["source"] = "temp"
                node_img["image"] = image
                node_img["url"] = ""

            # VAE and CLIP — always injected from defaults
            vae_loader["inputs"]["vae_name"] = "wan_2.1_vae.safetensors"
            clip_loader["inputs"]["clip_name"] = "umt5-xxl-encoder-Q5_K_M.gguf"
            clip_loader["inputs"]["type"] = "wan"

            # Length — always injected (default 81)
            wan_img2vid["inputs"]["length"] = resolved_length

            # Negative prompt (optional) — inject into the user Negative Prompt node
            if resolved_neg:
                negative_prompt_node["inputs"]["text"] = resolved_neg

            # =================================================================
            # Inject per-path values
            # =================================================================
            path_details = []
            for path_name, cfg in path_configs.items():
                nodes = path_nodes[path_name]

                # Diffusion model
                resolved_dm = _resolve_diffusion_model_for_path(
                    parsed_diffusion, path_name, cfg["diffusion_model"]
                )
                nodes["unet"]["inputs"]["unet_name"] = resolved_dm

                # Sampler + KSampler params
                nodes["ksampler"]["inputs"]["sampler_name"] = cfg["sampler"]
                nodes["ksampler"]["inputs"]["scheduler"] = cfg["scheduler"]
                nodes["ksampler"]["inputs"]["steps"] = cfg["steps"]
                nodes["ksampler"]["inputs"]["cfg"] = cfg["cfg"]

                # Dual-specific KSampler params — only in dual paths
                if is_dual:
                    nodes["ksampler"]["inputs"]["start_at_step"] = cfg["start_at_step"]
                    nodes["ksampler"]["inputs"]["end_at_step"] = cfg["end_at_step"]
                    nodes["ksampler"]["inputs"]["add_noise"] = cfg["add_noise"]
                    nodes["ksampler"]["inputs"]["return_with_leftover_noise"] = cfg["return_with_leftover_noise"]

                # Override steps from user valve (default 4)
                nodes["ksampler"]["inputs"]["steps"] = resolved_steps

                # For dual-path, recalculate start/end from resolved steps (always even)
                if is_dual:
                    half = resolved_steps // 2
                    if path_name == "high":
                        nodes["ksampler"]["inputs"]["start_at_step"] = 0
                        nodes["ksampler"]["inputs"]["end_at_step"] = half
                    else:
                        nodes["ksampler"]["inputs"]["start_at_step"] = half
                        nodes["ksampler"]["inputs"]["end_at_step"] = 10000

                # ModelSamplingSD3
                nodes["msampling"]["inputs"]["shift"] = cfg["model_sampling_shift"]

                # NAG
                nodes["nag"]["inputs"]["nag_scale"] = cfg["nag_scale"]
                nodes["nag"]["inputs"]["nag_alpha"] = cfg["nag_alpha"]
                nodes["nag"]["inputs"]["nag_tau"] = cfg["nag_tau"]

                # LoRAs for this path
                path_loras = _filter_loras_for_path(parsed_loras, path_name)
                lora_slots = [k for k in nodes["lora"]["inputs"] if k.startswith("lora_")]
                # Fill used slots
                for i, item in enumerate(path_loras[:len(lora_slots)]):
                    slot = f"lora_{i + 1}"
                    if slot not in nodes["lora"]["inputs"]:
                        break
                    name = item["model"]
                    strength = float(item.get("strength", 1.0))
                    if bool(name) and strength != 0:
                        nodes["lora"]["inputs"][slot]["on"] = True
                        nodes["lora"]["inputs"][slot]["lora"] = name
                        nodes["lora"]["inputs"][slot]["strength"] = strength
                    else:
                        nodes["lora"]["inputs"][slot]["on"] = False
                        nodes["lora"]["inputs"][slot]["lora"] = ""
                        nodes["lora"]["inputs"][slot]["strength"] = 0
                # Disable remaining unused slots
                for j in range(len(path_loras), len(lora_slots)):
                    slot = f"lora_{j + 1}"
                    if slot in nodes["lora"]["inputs"]:
                        nodes["lora"]["inputs"][slot]["on"] = False
                        nodes["lora"]["inputs"][slot]["lora"] = ""
                        nodes["lora"]["inputs"][slot]["strength"] = 0

                # Build detail string for this path
                path_lora_str = json.dumps(path_loras) if path_loras else "(none)"
                path_details.append(
                    f"{path_name}: model={resolved_dm}, sampler={cfg['sampler']}, "
                    f"scheduler={cfg['scheduler']}, steps={cfg['steps']}, cfg={cfg['cfg']}, "
                    f"loras={path_lora_str}"
                )

            log.info(
                "Dispatching %s workflow to ComfyUI (%s) - prompt_len=%d, seed=%d, "
                "length=%s, image=%s, neg_prompt=%s, loras=%s, paths=[%s]",
                resolved_version,
                image_config.COMFYUI_BASE_URL,
                len(prompt),
                seed_arg,
                resolved_length,
                image,
                repr(resolved_neg) if resolved_neg else "(none)",
                json.dumps(parsed_loras) if parsed_loras else "(none)",
                "; ".join(path_details),
            )

            # =================================================================
            # Execute workflow via ComfyUI API
            # =================================================================
            comfy_base = image_config.COMFYUI_BASE_URL.rstrip("/")
            api_key = image_config.COMFYUI_API_KEY or ""

            async with httpx.AsyncClient() as client:
                prompt_id = await _comfyui_queue_prompt(
                    client, comfy_base, api_key, workflow
                )

                log.info("Video workflow queued - prompt_id=%s", prompt_id)

                try:
                    outputs = await _comfyui_wait_for_output(
                        client, comfy_base, api_key, prompt_id
                    )
                except asyncio.CancelledError:
                    log.info("Video cancelled by user - interrupting ComfyUI")
                    await _comfyui_interrupt(comfy_base, api_key)
                    raise

            # =================================================================
            # Extract video filename and build URL
            # =================================================================
            video_filename, video_type = _extract_video_filename(outputs, output_node_id)

            base = resolved_video_base_url.rstrip("/")
            video_url = f"{base}/api/view?filename={video_filename}&type={video_type}"

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2705 Video generated.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            # Rich UI embed (see DESIGN.md §6): terminal result → bare
            # HTMLResponse (no tuple) so the LLM receives the middleware's
            # generic message ("Embedded UI result is active and visible to
            # the user."). The player is self-contained and sizes itself
            # (65vh cap) after the video metadata loads; no download button
            # (maintainer decision, 2026-08-04).
            player = self._build_video_player(video_url)
            return HTMLResponse(
                content=player, headers={"Content-Disposition": "inline"}
            )

        except asyncio.CancelledError:
            log.info("generate_video cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Video generation cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return (
                "The video generation was cancelled by the user. "
                "Do not retry. Acknowledge the cancellation and wait for their next request."
            )
        except Exception as e:
            log.exception("generate_video failed: %s", e)
            return f"Error generating video: {e}"

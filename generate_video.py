"""
title: Generate Video
author: A. Martin
description: Generate videos through ComfyUI (e.g. WAN2.1 text-to-video or image-to-video)
version: 2.0
"""

import asyncio
import json
import logging
import random as _random
import uuid
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


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
            f"Copy workflows/{filename} to that path."
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
# TOOL
# =============================================================================

class Tools:
    """
    Generate Video - animate an image into a video (image-to-video).

    Use when the user requests to animate an image into a video. Pass the
    image reference via the `image` parameter — either a filename from a
    previous generation (e.g. "abc123.png") or a direct URL to an external
    image (e.g. "https://..."). The tool auto-detects which one it is.

    prompt: Video description. Translate the user's request into English
        internally, then enrich with visual motion details without changing
        the subject or scene.
    image: Filename from a previous generation (e.g. "abc123.png"), or a
        direct URL to an external image to animate ("https://...").
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        model_name: str = Field(
            default="",
            description="Model/checkpoint name for video generation. Overrides the workflow default. Leave empty to use what's in the workflow JSON.",
        )
        lora: str = Field(
            default="",
            description="LoRA name for video style. Leave empty for no LoRA.",
        )
        length: int = Field(
            default=0,
            description="Number of frames / video length. 0 = use workflow default.",
        )
        negative_prompt: str = Field(
            default="",
            description="Negative prompt. Leave empty to use the workflow default.",
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for video links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        model_name: str = Field(
            default="",
            description="Your preferred model/checkpoint for video. Overrides the admin valve and the workflow default.",
        )
        lora: str = Field(
            default="",
            description="Your preferred LoRA for video style. Overrides the admin valve.",
        )
        length: int = Field(
            default=0,
            description="Number of frames / video length. 0 = use admin valve or workflow default.",
        )
        negative_prompt: str = Field(
            default="",
            description="Your preferred negative prompt. Leave empty to use the admin valve or workflow default.",
        )
        seed: int = Field(
            default=-1,
            description="Seed. -1 = random, >=0 = fixed seed for reproducibility.",
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Override the admin valve or COMFYUI_BASE_URL for video links.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

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

        prompt: Video description in English, enriched with motion details.
        image: Filename from a previous generation (e.g. "abc123.png")
            or a direct URL to an external image ("https://...").
            Auto-detects which mode to use.
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
            # Resolve valves: UserValves > AdminValves > workflow default
            # =================================================================
            user_valves = (__user__ or {}).get("valves", None)

            # Model: UserValves > AdminValves > leave workflow default
            resolved_model = (
                user_valves.model_name if user_valves and user_valves.model_name
                else self.valves.model_name or ""
            )

            # LoRA: UserValves > AdminValves > leave workflow default
            resolved_lora = (
                user_valves.lora if user_valves and user_valves.lora
                else self.valves.lora or ""
            )

            # Length: UserValves > AdminValves > leave workflow default
            user_length = user_valves.length if user_valves and user_valves.length else 0
            resolved_length = user_length or self.valves.length or 0

            # Negative prompt: UserValves > AdminValves > leave workflow default
            resolved_neg = (
                user_valves.negative_prompt if user_valves and user_valves.negative_prompt
                else self.valves.negative_prompt or ""
            )

            # Seed: UserValve. -1 = random, >=0 = fixed
            user_seed = int(user_valves.seed) if user_valves and user_valves.seed != -1 else -1
            seed_arg = _random.randint(0, _COMFY_SEED_MAX) if user_seed == -1 else min(user_seed, _COMFY_SEED_MAX)

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
            raw_workflow = _load_workflow(__id__, "generate_video.json")
            workflow = json.loads(raw_workflow)

            # Resolve workflow nodes by _meta.title (unique identifiers)
            _, positive_prompt = _resolve_node(workflow, "Positive Prompt")
            _, negative_prompt = _resolve_node(workflow, "Negative Prompt")
            _, load_image = _resolve_node(workflow, "Load Image (URL/Path)")
            _, unet_loader = _resolve_node(workflow, "Load Diffusion Model")
            _, lora_node = _resolve_node(workflow, "Power Lora Loader (rgthree)")
            _, wan_img2vid = _resolve_node(workflow, "WanImageToVideo")
            _, easy_seed = _resolve_node(workflow, "EasySeed")
            output_node_id, _ = _resolve_node(workflow, "Output MP4")

            # =================================================================
            # Inject dynamic values (formerly done via {{PLACEHOLDER}})
            # =================================================================
            positive_prompt["inputs"]["text"] = prompt
            easy_seed["inputs"]["seed"] = seed_arg

            # =================================================================
            # Configure image source — post-parse
            # =================================================================
            node_img = load_image["inputs"]
            parsed = urlparse(image)
            if parsed.scheme and parsed.netloc:
                # URL mode — image is optional, remove it to avoid validation
                # against the temp files list
                node_img["source"] = "url"
                node_img["url"] = image
                node_img.pop("image", None)
                node_img.pop("Choose file to upload", None)
            else:
                node_img["source"] = "temp"
                node_img["image"] = image
                node_img["url"] = ""

            # =================================================================
            # Apply optional overrides (only when valve is non-empty)
            # =================================================================
            if resolved_model:
                unet_loader["inputs"]["unet_name"] = resolved_model
            if resolved_lora:
                lora_node["inputs"]["lora_1"]["on"] = True
                lora_node["inputs"]["lora_1"]["lora"] = resolved_lora
            if resolved_length:
                wan_img2vid["inputs"]["length"] = resolved_length
            if resolved_neg:
                negative_prompt["inputs"]["text"] = resolved_neg

            log.info(
                "Dispatching video workflow to ComfyUI (%s) - prompt_len=%d, seed=%d, "
                "model=%s, lora=%s, length=%s, image=%s",
                image_config.COMFYUI_BASE_URL,
                len(prompt),
                seed_arg,
                resolved_model or "(workflow default)",
                resolved_lora or "(none — workflow default)",
                str(resolved_length) if resolved_length else "(workflow default)",
                image,
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

            # The HTML block the agent must emit to show the video
            html_block = (
                f'<html>\n'
                f'<style>\n'
                f'* {{ margin:0; padding:0; box-sizing:border-box; }}\n'
                f'body {{ background:#0d0d0d; display:flex; align-items:center; justify-content:center; min-height:100vh; }}\n'
                f'video {{ width:100%; height:100%; display:block; object-fit:contain; }}\n'
                f'</style>\n'
                f'<div style="background:#222; width:100%; height:100vh; display:flex;">\n'
                f'  <video controls autoplay muted loop playsinline style="width:100%;height:100%;object-fit:contain;">\n'
                f'    <source src="{video_url}" type="video/mp4">\n'
                f'  </video>\n'
                f'</div>\n'
                f'</html>'
            )

            return (
                f"{html_block}\n\n"
                "Wrap the HTML block above in triple backticks and include it in your response "
                "so the frontend renders the video."
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

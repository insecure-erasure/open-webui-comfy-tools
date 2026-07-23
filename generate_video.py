"""
title: Generate Video
author: A. Martin
description: Generate videos through ComfyUI (e.g. WAN2.1 text-to-video or image-to-video)
version: 1.0
"""

import asyncio
import json
import logging
import uuid
from urllib.parse import urlparse, parse_qs

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# =============================================================================
# Workflow JSON - PASTE YOUR VIDEO WORKFLOW HERE
# =============================================================================
# Export your video workflow from ComfyUI (WAN2.1, VideoCrafter, etc.)
# and paste it as the value of _VIDEO_WORKFLOW_JSON_RAW below.
# Then update the NODE_* constants with the correct node IDs.
#
# Expected workflow structure (example for WAN2.1 I2V):
#   - LoadImage node: reads the input image for animation
#   - CLIP / text encoding nodes: process the prompt
#   - WAN2.1 I2V node: generates the video
#   - VHS VideoCombine node: saves the video file (provides the filename)
#
_VIDEO_WORKFLOW_JSON_RAW = r"""{
  "1": {
    "inputs": {
      "frames": 81,
      "fps": 16,
      "width": 832,
      "height": 480,
      "seed": 0,
      "steps": 20,
      "cfg": 4.0,
      "prompt": "",
      "image": [""]
    },
    "class_type": "WanVideoI2V",
    "_meta": {
      "title": "WAN2.1 Image to Video"
    }
  },
  "2": {
    "inputs": {
      "images": ["1", 0],
      "frame_rate": 16,
      "loop_count": 0,
      "filename_prefix": "wan21_output",
      "format": "video/h264-mp4",
      "pingpong": false,
      "save_output": true,
      "videocodec": "libx264",
      "audio": null
    },
    "class_type": "VHS_VideoCombine",
    "_meta": {
      "title": "Video Combine"
    }
  }
}
"""

_VIDEO_WORKFLOW_JSON: dict = json.loads(_VIDEO_WORKFLOW_JSON_RAW)

# =============================================================================
# Node IDs - UPDATE THESE TO MATCH YOUR WORKFLOW
# =============================================================================
# After pasting your workflow above, set these to the correct node IDs:
NODE_LOAD_IMAGE: str = ""      # LoadImage node ID (for I2V; leave "" for T2V)
NODE_GENERATE: str = "1"       # Main video generation node ID
NODE_OUTPUT: str = "2"         # Node that saves the video file (e.g. VHS_VideoCombine)


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


def _extract_video_filename(outputs: dict, output_node_id: str) -> str:
    """
    Extract the video filename from the workflow outputs.

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
            if filename:
                return filename

    raise RuntimeError(
        f"Could not find a video filename in output node {output_node_id}. "
        f"Available outputs: {json.dumps(node_output, indent=2)}"
    )


# =============================================================================
# TOOL
# =============================================================================

class Tools:
    """
    Generate Video - generate a video through ComfyUI (text-to-video or image-to-video).

    Activate this tool from the tool selector in the chat input.

    Use this when the user asks to generate a video, animate an image,
    or create a video from a prompt.

    **Parameters for the agent:**

    prompt: The video description. Translate the user's request into English
        internally, then enrich with visual motion details without changing
        the subject or scene.
    image_filename (optional): The image_filename from a previous
        smart_generate_image or enhance_image response. Pass it as-is
        to animate an image that was just generated.

    --- Available Valves ---
    comfyui_image_base_url (admin / user):
        Public base URL for video links. If empty, defaults
        to COMFYUI_BASE_URL from Admin Panel > Settings > Images.
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for video links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for video links. Overrides the admin valve and COMFYUI_BASE_URL.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def generate_video(
        self,
        prompt: str,
        image_filename: str | None = None,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
    ):
        """
        Generate a video through ComfyUI (text-to-video or image-to-video).

        Use this when the user asks to generate a video or animate an image.

        prompt: Video description in English, enriched with visual motion details.
        image_filename (optional): The image_filename from smart_generate_image
            or enhance_image. Pass as-is for image-to-video animation.
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
            # Resolve video base URL (same pattern as enhance_image)
            #   UserValves > AdminValves > COMFYUI_BASE_URL
            # =================================================================
            user_valves = (__user__ or {}).get("valves", None)
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
            # Build the workflow inline
            # =================================================================
            workflow = dict(_VIDEO_WORKFLOW_JSON)

            # Inject the prompt into the generation node
            workflow[NODE_GENERATE]["inputs"]["prompt"] = prompt

            # Inject the input image filename for I2V (if provided)
            if image_filename and NODE_LOAD_IMAGE:
                workflow[NODE_LOAD_IMAGE]["inputs"]["image"] = image_filename
            elif image_filename and not NODE_LOAD_IMAGE:
                log.warning(
                    "image_filename was provided but NODE_LOAD_IMAGE is not set "
                    "in the workflow config. The image will be ignored."
                )

            log.info(
                "Dispatching video workflow to ComfyUI (%s) - prompt_len=%d, image=%s",
                image_config.COMFYUI_BASE_URL,
                len(prompt),
                image_filename or "(none)",
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
            video_filename = _extract_video_filename(outputs, NODE_OUTPUT)

            base = resolved_video_base_url.rstrip("/")
            video_url = f"{base}/api/view?filename={video_filename}"

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
                f'<style>\n'
                f'* {{ margin:0; padding:0; box-sizing:border-box; }}\n'
                f'body {{ background:#0d0d0d; display:flex; align-items:center; justify-content:center; min-height:100vh; }}\n'
                f'</style>\n'
                f'<div style="background:#222">\n'
                f'  <video controls autoplay muted loop playsinline style="width:100%;display:block">\n'
                f'    <source src="{video_url}" type="video/mp4">\n'
                f'  </video>\n'
                f'</div>'
            )

            return (
                f"video_md: {html_block}\n"
                f"video_filename: {video_filename}\n\n"
                "Copy the video_md HTML block exactly as shown above into your response "
                "to display the video player. Do not modify it."
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

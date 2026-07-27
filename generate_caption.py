"""
title: Generate Caption
author: A. Martin
description: Generate a detailed caption for an image using Florence-2
version: 1.0
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# =============================================================================
# ComfyUI constants
# =============================================================================
_COMFY_QUEUE_MAX_RETRIES = 120       # ~2 min at 1s intervals
_COMFY_QUEUE_POLL_INTERVAL = 1.0     # seconds


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
# ComfyUI API helpers
# =============================================================================

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
    """Poll /history/{prompt_id} until the workflow completes. Returns the full history entry."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    history_url = f"{base_url.rstrip('/')}/history/{prompt_id}"

    for attempt in range(_COMFY_QUEUE_MAX_RETRIES):
        resp = await client.get(history_url, headers=headers, timeout=10)
        resp.raise_for_status()
        history = resp.json()

        if prompt_id in history and history[prompt_id].get("outputs"):
            return history[prompt_id]

        # Key not in history yet or no outputs → still queued/processing
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


def _extract_caption(outputs: dict, output_node_id: str) -> str:
    """
    Extract the caption text from the workflow outputs.

    The ShowText|pysssss node exposes the caption text under the "string" key.
    Falls back to any available string/text field.

    Returns the caption string.
    """
    node_output = outputs.get(output_node_id, {})

    # ShowText|pysssss stores the text under "string"
    caption = node_output.get("string")
    if caption and isinstance(caption, str):
        return caption.strip()

    # Fallback: try "text" key
    caption = node_output.get("text")
    if caption and isinstance(caption, str):
        return caption.strip()

    # Worst case: dump the first string value found
    for key, value in node_output.items():
        if isinstance(value, str) and len(value) > 10:
            return value.strip()

    raise RuntimeError(
        f"Could not extract caption from output node {output_node_id}. "
        f"Available outputs: {json.dumps(node_output, indent=2)}"
    )


# =============================================================================
# TOOL
# =============================================================================

class Tools:
    """
    Generate a caption / description of an image.

    Only call when the user asks what an image depicts. Also use this
    before editing or enhancing an image to give yourself visual context —
    you cannot see the image directly. Pass an image filename or an URL.

    image: The filename previously generated from the smart_generate_image
        response, or a direct URL to an external image.
    """

    def __init__(self):
        self.citation = False

    async def generate_caption(
        self,
        image: str,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __id__: str = "",
    ):
        """
        Generate a caption / description of an image.

        Only call when the user asks what an image depicts. Also use this
        before editing or enhancing an image to give yourself visual context —
        you cannot see the image directly. Pass an image filename or an URL.
        """
        if __request__ is None:
            log.error("generate_caption called without request context")
            return "Error: The tool could not be initialized."

        try:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\U0001f5bc\ufe0f Generating caption...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            from open_webui.routers.images import get_image_config

            image_config = await get_image_config()

            # =================================================================
            # Build the workflow: load from cache and parse
            # =================================================================
            raw_workflow = _load_workflow(__id__, "generate_caption.json")
            workflow = json.loads(raw_workflow)

            # =================================================================
            # Resolve nodes
            # =================================================================
            _, load_image = _resolve_node(workflow, "Load Image (URL/Path)")
            output_node_id, _ = _resolve_node(workflow, "Show Text \U0001f40d")

            # =================================================================
            # Configure image source — auto-detect URL vs filename
            # =================================================================
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

            log.info(
                "Dispatching caption workflow to ComfyUI (%s) - %s=%s",
                image_config.COMFYUI_BASE_URL,
                "url" if parsed.scheme and parsed.netloc else "file",
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

                log.info("Caption workflow queued - prompt_id=%s", prompt_id)

                try:
                    history_entry = await _comfyui_wait_for_output(
                        client, comfy_base, api_key, prompt_id
                    )
                except asyncio.CancelledError:
                    log.info("Caption cancelled by user - interrupting ComfyUI")
                    await _comfyui_interrupt(comfy_base, api_key)
                    raise

            # =================================================================
            # Extract caption text
            # =================================================================
            outputs = history_entry["outputs"]
            caption = _extract_caption(outputs, output_node_id)

            log.info(
                "Caption generated - prompt_id=%s, length=%d chars",
                prompt_id,
                len(caption),
            )

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2705 Caption generated.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            return caption

        except asyncio.CancelledError:
            log.info("generate_caption cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Caption generation cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return (
                "The caption generation was cancelled by the user. "
                "Do not retry. Acknowledge the cancellation and wait for their next request."
            )
        except Exception as e:
            log.exception("generate_caption failed: %s", e)
            return f"Error generating caption: {e}"

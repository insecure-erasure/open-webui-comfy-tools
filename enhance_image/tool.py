"""
title: Enhance Image
author: Insecure Erasure
description: Enhance / upscale a previously generated image using SeedVR2
version: 1.0
"""

import asyncio
import json
import logging
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
            f"Copy {filename} from the tool's directory to that path."
        )

    log.info("Loading workflow from %s", workflow_path)
    return workflow_path.read_text(encoding='utf-8')


class Tools:
    """
    Enhance / upscale a previously generated image.

    Only call when the user explicitly asks to enhance or upscale
    an image. Pass an image filename or an URL.

    image: The filename previously generated from the smart_generate_image
        response, or a direct URL to an external image.
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links. Overrides the admin valve and COMFYUI_BASE_URL.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def enhance_image(
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
        Enhance / upscale a previously generated image.

        Only call when the user explicitly asks to enhance or upscale
        an image. Pass an image filename or an URL.

        :param image: The filename previously generated from the smart_generate_image response, or a direct URL to an external image.
        """
        if __request__ is None:
            log.error("enhance_image called without request context")
            return "Error: The tool could not be initialized."

        try:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\U0001f52e Enhancing image...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            from open_webui.routers.images import get_image_config
            from open_webui.utils.images.comfyui import (
                ComfyUICreateImageForm,
                ComfyUIWorkflow,
                comfyui_create_image,
            )
            import uuid

            image_config = await get_image_config()

            # =================================================================
            # Resolve image base URL for the output link
            #   UserValves > AdminValves > COMFYUI_BASE_URL
            # =================================================================
            user_valves = (__user__ or {}).get("valves", None)
            user_image_base_url = (
                user_valves.comfyui_image_base_url
                if user_valves and user_valves.comfyui_image_base_url
                else ""
            )
            resolved_image_base_url = (
                user_image_base_url
                or self.valves.comfyui_image_base_url
                or image_config.COMFYUI_BASE_URL
            )

            # =================================================================
            # Build the workflow: load from cache and parse
            # =================================================================
            raw_workflow = _load_workflow(__id__, "enhance_image.json")
            workflow = json.loads(raw_workflow)

            # Configure image source — auto-detect URL vs filename
            _, load_image = _resolve_node(workflow, "Load Image (URL/Path)")
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
                "Dispatching enhance workflow to ComfyUI (%s) - %s=%s",
                image_config.COMFYUI_BASE_URL,
                "url" if parsed.scheme and parsed.netloc else "file",
                image,
            )

            cf_form = ComfyUICreateImageForm(
                **{
                    "prompt": "",
                    "width": "1",
                    "height": "1",
                    "n": 1,
                    "workflow": ComfyUIWorkflow(
                        **{
                            "workflow": json.dumps(workflow),
                            "nodes": [],
                        }
                    ),
                }
            )

            # =================================================================
            # Execute workflow
            # =================================================================
            try:
                res = await comfyui_create_image(
                    None,
                    cf_form,
                    str(uuid.uuid4()),
                    image_config.COMFYUI_BASE_URL,
                    image_config.COMFYUI_API_KEY,
                )
            except asyncio.CancelledError:
                log.info("Enhance cancelled by user - interrupting ComfyUI")
                try:
                    interrupt_url = f"{image_config.COMFYUI_BASE_URL.rstrip('/')}/interrupt"
                    headers = {}
                    if image_config.COMFYUI_API_KEY:
                        headers["Authorization"] = f"Bearer {image_config.COMFYUI_API_KEY}"
                    async with httpx.AsyncClient() as client:
                        await client.post(interrupt_url, headers=headers, timeout=5)
                except Exception:
                    log.warning("Failed to interrupt ComfyUI", exc_info=True)
                raise

            if res is None or not res.get("data"):
                log.error("ComfyUI returned no image data")
                raise RuntimeError("ComfyUI returned no image data")

            # =================================================================
            # Build output URLs (same logic as smart_generate_image)
            # =================================================================
            images = []
            comfy_base = image_config.COMFYUI_BASE_URL.rstrip("/")
            for img in res["data"]:
                raw_url = img["url"]
                if raw_url.startswith("/"):
                    base = resolved_image_base_url.rstrip("/")
                    enhanced_url = f"{base}{raw_url}"
                elif raw_url.startswith(comfy_base):
                    enhanced_url = raw_url.replace(comfy_base, resolved_image_base_url.rstrip("/"), 1)
                else:
                    enhanced_url = raw_url
                images.append({"url": enhanced_url})

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2705 Image enhanced.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            enhanced_url = images[0]["url"] if images else None

            # Extract filename from URL (same as smart_generate_image)
            parsed = urlparse(enhanced_url)
            params = parse_qs(parsed.query)
            enhanced_filename = params.get("filename", ["unknown"])[0]

            return (
                f"image_md: ![Enhanced image]({enhanced_url})\n"
                f"image_filename: {enhanced_filename}\n\n"
                "Use image_md to display the enhanced image in your response."
            )

        except asyncio.CancelledError:
            log.info("enhance_image cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Image enhancement cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return (
                "The image enhancement was cancelled by the user. "
                "Do not retry. Acknowledge the cancellation and wait for their next request."
            )
        except Exception as e:
            log.exception("enhance_image failed: %s", e)
            return f"Error enhancing image: {e}"

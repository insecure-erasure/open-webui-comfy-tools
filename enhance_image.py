"""
title: Enhance Image
author: A. Martin
description: Enhance / upscale a previously generated image using SeedVR2
version: 1.0
"""

import asyncio
import json
import logging

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# =============================================================================
# Workflow JSON — SeedVR2 upscale (base64 encoded from ComfyUI export)
# =============================================================================
_ENHANCE_WORKFLOW_B64 = "ewogICI2MSI6IHsKICAgICJpbnB1dHMiOiB7CiAgICAgICJpbWFnZXMiOiBbCiAgICAgICAgIjQyMyIsCiAgICAgICAgMAogICAgICBdCiAgICB9LAogICAgImNsYXNzX3R5cGUiOiAiUHJldmlld0ltYWdlIiwKICAgICJfbWV0YSI6IHsKICAgICAgInRpdGxlIjogIlByZXZpZXcgSW1hZ2UiCiAgICB9CiAgfSwKICAiNDIxIjogewogICAgImlucHV0cyI6IHsKICAgICAgIm1vZGVsIjogImVtYV92YWVfZnAxNi5zYWZldGVuc29ycyIsCiAgICAgICJkZXZpY2UiOiAiY3VkYTowIiwKICAgICAgImVuY29kZV90aWxlZCI6IHRydWUsCiAgICAgICJlbmNvZGVfdGlsZV9zaXplIjogMTAyNCwKICAgICAgImVuY29kZV90aWxlX292ZXJsYXAiOiAxMjgsCiAgICAgICJkZWNvZGVfdGlsZWQiOiB0cnVlLAogICAgICAiZGVjb2RlX3RpbGVfc2l6ZSI6IDEwMjQsCiAgICAgICJkZWNvZGVfdGlsZV9vdmVybGFwIjogMTI4LAogICAgICAidGlsZV9kZWJ1ZyI6ICJmYWxzZSIsCiAgICAgICJvZmZsb2FkX2RldmljZSI6ICJub25lIiwKICAgICAgImNhY2hlX21vZGVsIjogZmFsc2UKICAgIH0sCiAgICAiY2xhc3NfdHlwZSI6ICJTZWVkVlIyTG9hZFZBRU1vZGVsIiwKICAgICJfbWV0YSI6IHsKICAgICAgInRpdGxlIjogIlNlZWRWUjIgKERvd24pTG9hZCBWQUUgTW9kZWwiCiAgICB9CiAgfSwKICAiNDIzIjogewogICAgImlucHV0cyI6IHsKICAgICAgImJsZW5kX2ZhY3RvciI6IDAuMTUsCiAgICAgICJibGVuZF9tb2RlIjogIm5vcm1hbCIsCiAgICAgICJpbWFnZTEiOiBbCiAgICAgICAgIjQyNCIsCiAgICAgICAgMAogICAgICBdCiAgICB9LAogICAgImNsYXNzX3R5cGUiOiAiSW1hZ2VCbGVuZCIsCiAgICAiX21ldGEiOiB7CiAgICAgICJ0aXRsZSI6ICJJbWFnZSBCbGVuZCIKICAgIH0KICB9LAogICI0MjQiOiB7CiAgICAiaW5wdXRzIjogewogICAgICAic2VlZCI6IDAsCiAgICAgICJyZXNvbHV0aW9uIjogMjA0OCwKICAgICAgIm1heF9yZXNvbHV0aW9uIjogMjA0OCwKICAgICAgImJhdGNoX3NpemUiOiAxLAogICAgICAidW5pZm9ybV9iYXRjaF9zaXplIjogZmFsc2UsCiAgICAgICJjb2xvcl9jb3JyZWN0aW9uIjogImxhYiIsCiAgICAgICJ0ZW1wb3JhbF9vdmVybGFwIjogMCwKICAgICAgInByZXBlbmRfZnJhbWVzIjogMCwKICAgICAgImlucHV0X25vaXNlX3NjYWxlIjogMC4wMSwKICAgICAgImxhdGVudF9ub2lzZV9zY2FsZSI6IDAsCiAgICAgICJvZmZsb2FkX2RldmljZSI6ICJjcHUiLAogICAgICAiZW5hYmxlX2RlYnVnIjogZmFsc2UsCiAgICAgICJpbWFnZSI6IFsKICAgICAgICAiNDI2IiwKICAgICAgICAwCiAgICAgIF0sCiAgICAgICJkaXQiOiBbCiAgICAgICAgIjQyNSIsCiAgICAgICAgMAogICAgICBdLAogICAgICAidmFlIjogWwogICAgICAgICI0MjEiLAogICAgICAgIDAKICAgICAgXQogICAgfSwKICAgICJjbGFzc190eXBlIjogIlNlZWRWUjJWaWRlb1Vwc2NhbGVyIiwKICAgICJfbWV0YSI6IHsKICAgICAgInRpdGxlIjogIlNlZWRWUjIgVmlkZW8gVXBzY2FsZXIgKHYyLjUuMjQpIgogICAgfQogIH0sCiAgIjQyNSI6IHsKICAgICJpbnB1dHMiOiB7CiAgICAgICJtb2RlbCI6ICJzZWVkdnIyX2VtYV83Yi1RNF9LX00uZ2d1ZiIsCiAgICAgICJkZXZpY2UiOiAiY3VkYTowIiwKICAgICAgImJsb2Nrc190b19zd2FwIjogMzYsCiAgICAgICJzd2FwX2lvX2NvbXBvbmVudHMiOiBmYWxzZSwKICAgICAgIm9mZmxvYWRfZGV2aWNlIjogIm5vbmUiLAogICAgICAiY2FjaGVfbW9kZWwiOiBmYWxzZSwKICAgICAgImF0dGVudGlvbl9tb2RlIjogInNkcGEiCiAgICB9LAogICAgImNsYXNzX3R5cGUiOiAiU2VlZFZSMkxvYWREaVRNb2RlbCIsCiAgICAiX21ldGEiOiB7CiAgICAgICJ0aXRsZSI6ICJTZWVkVlIyIChEb3duKUxvYWQgRGlUIE1vZGVsIgogICAgfQogIH0sCiAgIjQyNiI6IHsKICAgICJpbnB1dHMiOiB7CiAgICAgICJzb3VyY2UiOiAidGVtcCIsCiAgICAgICJ1cmwiOiAiIiwKICAgICAgImltYWdlIjogIkNvbWZ5VUlfdGVtcF91aHlneV8wMDAwMV8ucG5nIiwKICAgICAgIkNob29zZSBmaWxlIHRvIHVwbG9hZCI6IG51bGwKICAgIH0sCiAgICAiY2xhc3NfdHlwZSI6ICJMb2FkSW1hZ2VCeVVybE9yUGF0aCIsCiAgICAiX21ldGEiOiB7CiAgICAgICJ0aXRsZSI6ICJMb2FkIEltYWdlIChVUkwvUGF0aCkiCiAgICB9CiAgfQp9"

ENHANCE_WORKFLOW_JSON: dict = json.loads(base64.b64decode(_ENHANCE_WORKFLOW_B64).decode())

# =============================================================================
# Node IDs from the workflow
# =============================================================================
NODE_LOAD_IMAGE: str = "426"
NODE_ENHANCE: str = "424"
NODE_OUTPUT: str = "61"


class Tools:
    """
    Enhance Image - upscale or enhance a previously generated image using SeedVR2.

    Activate this tool from the tool selector in the chat input.

    Only use when the user explicitly asks to enhance, upscale, or improve
    an image that was just generated. Pass the image_filename from the
    smart_generate_image response — do not modify it.

    --- Available Valves ---
    comfyui_image_base_url (admin / user):
        Public base URL for the generated image links. If empty, defaults
        to COMFYUI_BASE_URL from Admin Panel > Settings > Images.
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
        image_filename: str,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
    ):
        """
        Enhance / upscale a previously generated image using SeedVR2.

        Only call when the user explicitly asks to enhance or upscale
        an image. Pass the image_filename from the smart_generate_image
        response as-is — do not modify it.

        image_filename: The filename from the smart_generate_image response.
        """
        if __request__ is None:
            log.error("enhance_image called without request context")
            return "Error: The tool could not be initialized."

        try:
            filename = image_filename

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
            # Build the workflow inline
            # =================================================================
            workflow = dict(ENHANCE_WORKFLOW_JSON)

            # Inject the filename into the LoadImageByUrlOrPath node
            workflow[NODE_LOAD_IMAGE]["inputs"]["image"] = filename

            log.info(
                "Dispatching enhance workflow to ComfyUI (%s) — file=%s",
                image_config.COMFYUI_BASE_URL,
                filename,
            )

            cf_form = ComfyUICreateImageForm(
                **{
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
                log.info("Enhance cancelled by user — interrupting ComfyUI")
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

            return (
                "Image enhanced successfully.\n\n"
                "Display the enhanced image in your response like this:\n"
                f"![Enhanced image]({enhanced_url})"
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

"""
title: Smart Generate Image
author: A. Martin
description: Generate images through ComfyUI with seed, model, size, and steps control
version: 2.4
"""

import asyncio
import logging
import math
import random as _random

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Shared dropdown options for the steps valve (15 down to 1, plus "System default")
_STEPS_OPTIONS = [
    {"value": str(i), "label": str(i)}
    for i in range(15, 0, -1)
]
_STEPS_OPTIONS.insert(0, {"value": "0", "label": "System default"})

_STEPS_FIELD = Field(
    default="0",
    description="Inference steps.",
    json_schema_extra={
        "input": {
            "type": "select",
            "options": _STEPS_OPTIONS,
        }
    },
)

# =============================================================================
# MONKEY PATCH 1: Add seed field to CreateImageForm
# =============================================================================
from open_webui.routers import images as images_router


class PatchedCreateImageForm(images_router.CreateImageForm):
    seed: int | None = Field(
        default=None,
        description="Seed for reproducibility (defaults to 0 in the tool)",
    )
    comfyui_image_base_url: str | None = Field(
        default=None,
        description="Base URL for image URLs (overrides COMFYUI_BASE_URL for display)",
    )


images_router.CreateImageForm = PatchedCreateImageForm
images_router.GenerateImageForm = PatchedCreateImageForm

log.info("MONKEY PATCH 1: CreateImageForm patched with seed field")

# =============================================================================
# MONKEY PATCH 2: Make _apply_workflow_nodes ignore None values
# =============================================================================
from open_webui.utils.images import comfyui as comfyui_module

NODE_TYPE_INPUT_KEYS = {
    "prompt": "text",
    "model": "unet_name",
    "width": "string_a",
    "height": "string_b",
    "steps": "steps",
    "seed": "seed",
}


def patched_apply_workflow_nodes(workflow, nodes, model, payload):
    """Like the original, but skips assignment when the payload value is None."""
    for node in nodes:
        if not node.type:
            continue

        input_key = NODE_TYPE_INPUT_KEYS.get(node.type, node.key)

        if node.type == "model":
            if model is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][input_key] = model

        elif node.type == "prompt":
            if payload.prompt is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][input_key] = payload.prompt

        elif node.type == "width":
            if payload.width is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][input_key] = payload.width

        elif node.type == "height":
            if payload.height is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][input_key] = payload.height

        elif node.type == "steps":
            if payload.steps is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][input_key] = payload.steps

        elif node.type == "seed":
            if payload.seed is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][input_key] = payload.seed

        elif node.type == "n":
            if payload.n is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][input_key] = payload.n

        elif node.type == "image":
            if payload.image is not None:
                if isinstance(payload.image, list):
                    for idx, node_id in enumerate(node.node_ids):
                        if idx < len(payload.image):
                            workflow[node_id]["inputs"][input_key] = payload.image[idx]
                else:
                    for node_id in node.node_ids:
                        workflow[node_id]["inputs"][input_key] = payload.image

        else:
            if node.value is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][input_key] = node.value


comfyui_module._apply_workflow_nodes = patched_apply_workflow_nodes

log.info("MONKEY PATCH 2: _apply_workflow_nodes patched to ignore None values")

# =============================================================================
# MONKEY PATCH 3: Override image_generations for ComfyUI
# =============================================================================
_original_image_generations = images_router.image_generations


async def patched_image_generations(request, form_data, metadata=None, user=None):
    from open_webui.routers.images import get_image_config
    import uuid

    image_config = await get_image_config()
    engine = image_config.IMAGE_GENERATION_ENGINE

    if engine != "comfyui":
        return await _original_image_generations(request, form_data, metadata, user)

    log.info("Image generation requested")

    size = "512x512"
    if image_config.IMAGE_SIZE and "x" in image_config.IMAGE_SIZE:
        size = image_config.IMAGE_SIZE
    if form_data.size and "x" in form_data.size:
        size = form_data.size
    width, height = tuple(map(int, size.split("x")))

    gcd = math.gcd(width, height)
    reduced_w = width // gcd
    reduced_h = height // gcd

    admin_default_model = await images_router.get_image_model(request)
    effective_model = (
        form_data.model if form_data.model is not None else admin_default_model
    )
    admin_steps = image_config.IMAGE_STEPS

    log.info(
        "Generating image: prompt_len=%d, size=%s, seed=%s, steps=%s, model=%s",
        len(form_data.prompt),
        size,
        form_data.seed,
        form_data.steps if form_data.steps is not None else "workflow_default",
        effective_model or "not_set",
    )

    # =========================================================================
    # Node bindings
    # =========================================================================
    from open_webui.utils.images.comfyui import (
        ComfyUICreateImageForm,
        ComfyUIWorkflow,
        comfyui_create_image,
    )

    configured_node_types = {
        node["type"] for node in image_config.COMFYUI_WORKFLOW_NODES
    }

    # =========================================================================
    # Payload build
    # =========================================================================
    data = {
        "prompt": form_data.prompt,
        "width": str(reduced_w),
        "height": str(reduced_h),
        "n": 1,
    }

    if form_data.seed is not None:
        data["seed"] = form_data.seed
        if "seed" not in configured_node_types:
            log.warning(
                "Seed=%d was provided but no 'seed' node binding is configured. "
                "The value will be ignored by ComfyUI.",
                form_data.seed,
            )

    if "steps" in configured_node_types:
        if form_data.steps is not None:
            data["steps"] = form_data.steps
        elif admin_steps is not None:
            data["steps"] = admin_steps

    if "model" in configured_node_types:
        data["model"] = effective_model

    log.info("Dispatching to ComfyUI (%s)", image_config.COMFYUI_BASE_URL)

    cf_form = ComfyUICreateImageForm(
        **{
            "workflow": ComfyUIWorkflow(
                **{
                    "workflow": image_config.COMFYUI_WORKFLOW,
                    "nodes": image_config.COMFYUI_WORKFLOW_NODES,
                }
            ),
            **data,
        }
    )

    try:
        res = await comfyui_create_image(
            effective_model,
            cf_form,
            str(uuid.uuid4()),
            image_config.COMFYUI_BASE_URL,
            image_config.COMFYUI_API_KEY,
        )
    except asyncio.CancelledError:
        log.info("Image generation cancelled by user — interrupting ComfyUI")
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
    except Exception as e:
        log.error("ComfyUI image generation failed: %s", e)
        raise

    if res is None or not res.get("data"):
        log.error("ComfyUI returned no image data")
        raise RuntimeError("ComfyUI returned no image data")

    # No download/re-upload - return the URL directly from
    # the image generation engine using the image base URL
    # resolved from: UserValves > AdminValves > COMFYUI_BASE_URL.
    #
    # PREREQUISITE: The image base URL must be accessible from the
    # user's browser (or from where the LLM renders the image).
    image_base_url = (
        form_data.comfyui_image_base_url
        if form_data.comfyui_image_base_url
        else image_config.COMFYUI_BASE_URL
    )
    images = []
    comfy_base = image_config.COMFYUI_BASE_URL.rstrip("/")
    for img in res["data"]:
        raw_url = img["url"]
        if raw_url.startswith("/"):
            base = image_base_url.rstrip("/")
            image_url = f"{base}{raw_url}"
        elif raw_url.startswith(comfy_base):
            # Absolute URL from ComfyUI — rewrite the host part
            image_url = raw_url.replace(comfy_base, image_base_url.rstrip("/"), 1)
        else:
            image_url = raw_url
        images.append({"url": image_url})

    log.info(
        "Generation complete - %d image(s) - %s",
        len(images),
        images[0]["url"],
    )
    return images


images_router.image_generations = patched_image_generations

log.info("MONKEY PATCH 3: image_generations patched for ComfyUI")

# =============================================================================
# TOOLS CLASS
#
# The built-in generate_image and this tool are independent.
# Activate or deactivate Smart Generate Image from the tool selector
# in the chat input.
#
# Images are NOT emitted via event emitter and NOT persisted to chat history.
# The LLM receives the image URL and renders it as markdown in its response.
# This avoids the "not vision capable" toast for non-vision models.
# =============================================================================


class Tools:
    """
    Smart Generate Image - generate images through ComfyUI with control over size.

    Activate this tool from the tool selector in the chat input.

    prompt: Image generation prompt. Translate the user's request into English
        internally, then enrich with visual details without changing the subject
        or scene. Do not add superfluous details. Write the final prompt in English.
    size (optional): Only provide when the user explicitly requests specific
        dimensions. Format as WxH (e.g., 2000x3000).
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        model_name: str = Field(
            default="",
            description="Model/checkpoint name. Overrides the Admin UI default. Leave empty to use the Admin UI setting.",
        )
        steps: str = _STEPS_FIELD
        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve and Admin UI)."""

        model_name: str = Field(
            default="",
            description="Your preferred model/checkpoint. Overrides the admin valve and the Admin UI setting.",
        )
        steps: str = _STEPS_FIELD
        comfyui_image_base_url: str = Field(
            default="",
            description="Override the admin valve or COMFYUI_BASE_URL for image links.",
        )
        seed: int = Field(
            default=-1,
            description="Seed. -1 = random, >=0 = fixed seed for reproducibility.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def smart_generate_image(
        self,
        prompt: str,
        size: str | None = None,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
    ):
        """
        Generate one image with optional control over size.

        prompt: Image generation prompt. Translate the user's request into English internally,
            then enrich with visual details without changing the subject or scene. Do not add
            superfluous details. Write the final prompt in English.
        size (optional): Only provide when the user explicitly requests specific
            dimensions. Format as WxH (e.g., 2000x3000).
        """
        if __request__ is None:
            log.error("smart_generate_image called without request context")
            return "Error: The tool could not be initialized."

        try:
            from open_webui.models.users import UserModel

            user = UserModel(**__user__) if __user__ else None

            # Resolve model: UserValves > AdminValves > Admin UI default
            user_valves = (__user__ or {}).get('valves', None)
            user_model = (
                user_valves.model_name if user_valves and user_valves.model_name else ""
            )
            resolved_model = user_model or self.valves.model_name or None

            # Resolve steps: UserValves > AdminValves > Admin UI > workflow default
            # All clamped against IMAGE_STEPS if set, or 15 as safety ceiling.
            from open_webui.routers.images import get_image_config

            image_config = await get_image_config()
            admin_steps_config = image_config.IMAGE_STEPS
            ceiling = 15 if admin_steps_config is None or admin_steps_config <= 0 else admin_steps_config

            user_valve_steps = int(user_valves.steps) if user_valves and user_valves.steps and user_valves.steps != "0" else 0
            admin_valve_steps = int(self.valves.steps) if self.valves.steps and self.valves.steps != "0" else 0

            resolved_steps = None
            if user_valve_steps > 0:
                resolved_steps = min(user_valve_steps, ceiling)
                if resolved_steps < user_valve_steps and __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "warning",
                                "content": f"\u26a0\ufe0f Steps clamped to {ceiling} (system limit).",
                            },
                        }
                    )
            elif admin_valve_steps > 0:
                resolved_steps = min(admin_valve_steps, ceiling)
                if resolved_steps < admin_valve_steps and __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "warning",
                                "content": f"\u26a0\ufe0f Steps clamped to {ceiling} (system limit).",
                            },
                        }
                    )
            elif admin_steps_config is not None and admin_steps_config > 0:
                resolved_steps = admin_steps_config

            # Resolve seed: UserValve. -1 = generate random, >=0 = fixed.
            user_seed = int(user_valves.seed) if user_valves and user_valves.seed != -1 else -1
            seed_arg = _random.randint(0, 0xFFFFFFFFFFFFFFFF) if user_seed == -1 else user_seed

            # Resolve image base URL: UserValves > AdminValves > COMFYUI_BASE_URL
            user_image_base_url = (
                user_valves.comfyui_image_base_url if user_valves and user_valves.comfyui_image_base_url else ""
            )
            resolved_image_base_url = user_image_base_url or self.valves.comfyui_image_base_url or image_config.COMFYUI_BASE_URL

            steps_label = str(resolved_steps) if resolved_steps else "workflow default"

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"\U0001f3a8 Generating image with {steps_label} steps...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            images = await patched_image_generations(
                request=__request__,
                form_data=PatchedCreateImageForm(
                    prompt=prompt,
                    model=resolved_model,
                    size=size,
                    steps=resolved_steps,
                    seed=seed_arg,
                    comfyui_image_base_url=resolved_image_base_url,
                ),
                user=user,
            )

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2705 Image generated.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            image_url = images[0]["url"] if images else None

            return f"Image generated successfully.\n\nDisplay the image in your response like this:\n![Generated image]({image_url})"

        except asyncio.CancelledError:
            log.info("smart_generate_image cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Image generation cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return "The image generation was cancelled by the user. Do not retry. Acknowledge the cancellation and wait for their next request."
        except Exception as e:
            log.exception("smart_generate_image failed: %s", e)
            return f"Error generating image: {e}"

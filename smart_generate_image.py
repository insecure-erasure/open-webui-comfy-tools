"""
title: Smart Generate Image
author: Abel
description: Generate images through ComfyUI with seed, model, size, and steps control
version: 2.2
"""

import json
import logging
import math
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# =============================================================================
# MONKEY PATCH 1: Add seed field to CreateImageForm
# =============================================================================
from open_webui.routers import images as images_router


class PatchedCreateImageForm(images_router.CreateImageForm):
    seed: int | None = Field(
        default=None,
        description="Seed for reproducibility (defaults to 0 in the tool)",
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

    # =========================================================================
    # LAYER 1 — Admin defaults
    # =========================================================================
    log.info("ComfyUI image generation requested — resolving Layer 1 defaults")

    size = "512x512"
    if image_config.IMAGE_SIZE and "x" in image_config.IMAGE_SIZE:
        size = image_config.IMAGE_SIZE
    if form_data.size and "x" in form_data.size:
        size = form_data.size
    width, height = tuple(map(int, size.split("x")))

    gcd = math.gcd(width, height)
    reduced_w = width // gcd
    reduced_h = height // gcd
    log.info(
        "Dimensions resolved: %s → gcd=%d → %dx%d",
        size,
        gcd,
        reduced_w,
        reduced_h,
    )

    admin_default_model = await images_router.get_image_model(request)
    effective_model = (
        form_data.model if form_data.model is not None else admin_default_model
    )
    admin_steps = image_config.IMAGE_STEPS
    log.info(
        "Model resolved: admin=%s, effective=%s | Admin steps: %s",
        admin_default_model,
        effective_model,
        admin_steps if admin_steps is not None else "not set",
    )

    # =========================================================================
    # LAYER 2 — Node bindings
    # =========================================================================
    from open_webui.utils.images.comfyui import (
        ComfyUICreateImageForm,
        ComfyUIWorkflow,
        comfyui_create_image,
    )

    configured_node_types = {
        node["type"] for node in image_config.COMFYUI_WORKFLOW_NODES
    }
    log.info(
        "Layer 2 — Configured node types: %s",
        sorted(configured_node_types),
    )

    # =========================================================================
    # LAYER 3 — Payload build
    # =========================================================================
    data = {
        "prompt": form_data.prompt,
        "width": str(reduced_w),
        "height": str(reduced_h),
        "n": 1,
    }

    seed = form_data.seed if form_data.seed is not None else 0
    data["seed"] = seed
    if "seed" not in configured_node_types:
        log.warning(
            "Seed=%d was provided but no 'seed' node binding is configured. "
            "The value will be ignored by ComfyUI.",
            seed,
        )

    if "steps" in configured_node_types:
        if form_data.steps is not None:
            data["steps"] = form_data.steps
            log.info("Steps overridden by LLM: %d", form_data.steps)
        elif admin_steps is not None:
            data["steps"] = admin_steps
            log.info("Steps from admin default: %d", admin_steps)
        else:
            log.info(
                "Steps node configured but no admin default set — "
                "ComfyUI will use its workflow default"
            )
    else:
        log.info("Steps omitted — no 'steps' node binding configured")

    if "model" in configured_node_types:
        data["model"] = effective_model
        log.info(
            "Model %s: %s",
            (
                "overridden by LLM"
                if form_data.model is not None
                else "from admin default"
            ),
            effective_model,
        )
    else:
        log.info("Model omitted — no 'model' node binding configured")

    log.info(
        "Layer 3 — Payload built: prompt_len=%d, width=%s, height=%s, "
        "seed=%d, steps=%s, model=%s",
        len(data["prompt"]),
        data["width"],
        data["height"],
        data["seed"],
        data.get("steps", "not_sent"),
        data.get("model", "not_sent"),
    )

    # =========================================================================
    # LAYER 4 — ComfyUI execution (modified: no local storage)
    # =========================================================================
    log.info("Layer 4 — Dispatching to ComfyUI")

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
    except Exception as e:
        log.error("ComfyUI image generation failed: %s", e)
        raise

    if res is None or not res.get("data"):
        log.error("ComfyUI returned no image data")
        raise RuntimeError("ComfyUI returned no image data")

    log.info("ComfyUI returned %d image(s)", len(res["data"]))

    # ═══════════════════════════════════════════════════════════════════════
    # NO descargamos ni re-subimos — devolvemos la URL directamente de
    # ComfyUI usando la Base URL configurada en Admin Panel > Settings > Images.
    #
    # PRERREQUISITO: COMFYUI_BASE_URL debe ser accesible desde el navegador
    # del usuario final (o desde donde el LLM renderice la imagen).
    #
    # Si usas Preview Image en ComfyUI, la imagen vive en el buffer interno
    # de ComfyUI y la URL es válida mientras el servidor no se reinicie.
    # Para persistencia más larga, considera añadir un nodo Save Image que
    # guarde en una carpeta servida estáticamente por nginx.
    # ═══════════════════════════════════════════════════════════════════════
    images = []
    for img in res["data"]:
        raw_url = img["url"]
        if raw_url.startswith("/"):
            base = image_config.COMFYUI_BASE_URL.rstrip("/")
            image_url = f"{base}{raw_url}"
        else:
            image_url = raw_url
        images.append({"url": image_url})

    log.info(
        "Image generation complete — %d image(s) returned directly "
        "from ComfyUI (no local storage)",
        len(images),
    )
    return images


images_router.image_generations = patched_image_generations

log.info(
    "MONKEY PATCH 3: image_generations patched for ComfyUI " "with 4-layer hierarchy"
)

# =============================================================================
# TOOLS CLASS
#
# The chip (📷) controls the built-in generate_image independently from this
# Activate or deactivate Smart Generate Image from the tool selector
# (⚙️) in the chat input.
#
# Images are NOT emitted via event emitter and NOT persisted to chat history.
# The LLM receives the image URL and renders it as markdown in its response.
# This avoids the "not vision capable" toast for non-vision models.
# =============================================================================


class Tools:
    """
    Smart Generate Image — generate images through ComfyUI with full control over seed, model, size, and steps.

    Activate this tool from the tool selector (⚙️) in the chat input.

    prompt: Image generation prompt. Translate the user's request into English
        internally, then enrich with visual details without changing the subject
        or scene. Do not add superfluous details. Write the final prompt in English.
    model (optional): Only provide when the user explicitly requests a
        specific model.
    size (optional): Only provide when the user explicitly requests specific
        dimensions. Format as WxH (e.g., 2000x3000).
    steps (optional): Only provide when the user explicitly requests a
        specific number of steps.
    seed (optional): Only provide when the user explicitly requests a
        specific seed.
    """

    class Valves:
        """No configuration valves needed — all settings come from Admin UI."""

        pass

    def __init__(self):
        self.citation = False

    async def generate_image_pro(
        self,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
        steps: int | None = None,
        seed: int = 0,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
    ):
        """
        Generate one image with optional control over model, seed, size, and steps.

        Display the generated image in your response using markdown:
            ![Generated image](image_url)

        prompt: Image generation prompt. Translate the user's request into English internally,
            then enrich with visual details without changing the subject or scene. Do not add
            superfluous details. Write the final prompt in English.
        model (optional): Only provide when the user explicitly requests a
            specific model.
        size (optional): Only provide when the user explicitly requests specific
            dimensions. Format as WxH (e.g., 2000x3000).
        steps (optional): Only provide when the user explicitly requests a
            specific number of steps.
        seed (optional): Only provide when the user explicitly
            requests a specific seed.
        """
        if __request__ is None:
            log.error("generate_image_pro called without request context")
            return json.dumps({"error": "Request context not available"})

        try:
            from open_webui.models.users import UserModel

            user = UserModel(**__user__) if __user__ else None

            log.info(
                "generate_image_pro called — prompt_len=%d, model=%s, "
                "size=%s, steps=%s, seed=%d",
                len(prompt),
                model,
                size,
                steps,
                seed,
            )

            images = await patched_image_generations(
                request=__request__,
                form_data=PatchedCreateImageForm(
                    prompt=prompt,
                    model=model,
                    size=size,
                    steps=steps,
                    seed=seed,
                ),
                user=user,
            )

            # Image is not emitted via event emitter and not persisted
            # to chat history. The LLM receives the URL and renders it
            # as markdown in its text response.
            image_url = images[0]["url"] if images else None

            log.info("Image generated — url=%s", image_url)

            return json.dumps(
                {
                    "status": "success",
                    "message": "Image generated successfully.",
                    "image_url": image_url,
                },
                ensure_ascii=False,
            )

        except Exception as e:
            log.exception("generate_image_pro failed: %s", e)
            return json.dumps({"error": str(e)})

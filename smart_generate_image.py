"""
title: Image Generator Pro
author: Abel
version: 2.0
"""

import json
import logging
import math
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# =============================================================================
# MONKEY PATCH 1: Add seed field to CreateImageForm
#
# The built-in CreateImageForm does not have a seed field.
# We subclass it to add one, then replace the original in the module so that
# any code importing CreateImageForm gets our patched version.
# =============================================================================
from open_webui.routers import images as images_router


class PatchedCreateImageForm(images_router.CreateImageForm):
    seed: int | None = Field(
        default=None,
        description="Seed for reproducibility (defaults to 0 in the tool)",
    )


images_router.CreateImageForm = PatchedCreateImageForm
images_router.GenerateImageForm = PatchedCreateImageForm

log.info(
    "MONKEY PATCH 1: CreateImageForm patched with seed field"
)

# =============================================================================
# MONKEY PATCH 2: Make _apply_workflow_nodes ignore None values
#
# The original function unconditionally overwrites workflow node inputs even
# when the payload value is None. This injects null into the workflow JSON
# for fields like steps when the LLM omits them.
#
# Our patched version skips assignment when the value is None, preserving
# whatever default the exported workflow JSON carries.
# =============================================================================
from open_webui.utils.images import comfyui as comfyui_module


def patched_apply_workflow_nodes(workflow, nodes, model, payload):
    """Like the original, but skips assignment when the payload value is None."""
    for node in nodes:
        if not node.type:
            continue

        if node.type == "model":
            if model is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key] = model

        elif node.type == "prompt":
            if payload.prompt is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][
                        node.key if node.key else "text"
                    ] = payload.prompt

        elif node.type == "width":
            if payload.width is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][
                        node.key if node.key else "width"
                    ] = payload.width

        elif node.type == "height":
            if payload.height is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][
                        node.key if node.key else "height"
                    ] = payload.height

        elif node.type == "steps":
            if payload.steps is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][
                        node.key if node.key else "steps"
                    ] = payload.steps

        elif node.type == "seed":
            if payload.seed is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key] = payload.seed

        elif node.type == "n":
            if payload.n is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][
                        node.key if node.key else "batch_size"
                    ] = payload.n

        elif node.type == "image":
            if payload.image is not None:
                if isinstance(payload.image, list):
                    for idx, node_id in enumerate(node.node_ids):
                        if idx < len(payload.image):
                            workflow[node_id]["inputs"][node.key] = (
                                payload.image[idx]
                            )
                else:
                    for node_id in node.node_ids:
                        workflow[node_id]["inputs"][node.key] = payload.image

        else:
            if node.value is not None:
                for node_id in node.node_ids:
                    workflow[node_id]["inputs"][node.key] = node.value


comfyui_module._apply_workflow_nodes = patched_apply_workflow_nodes

log.info(
    "MONKEY PATCH 2: _apply_workflow_nodes patched to ignore None values"
)

# =============================================================================
# MONKEY PATCH 3: Override image_generations for ComfyUI
#
# The original image_generations function does not support seed injection,
# GCD-based dimension reduction, or model override per-call.
#
# Our override organises the logic in four explicit layers:
#   Layer 1 — Admin defaults  (from Admin UI Settings)
#   Layer 2 — Node bindings   (from Workflow Nodes configuration)
#   Layer 3 — Payload build   (tool parameters filtered through Layer 2)
#   Layer 4 — ComfyUI call    (execution against the workflow JSON)
#
# For any non-ComfyUI engine we delegate transparently to the original.
# =============================================================================
_original_image_generations = images_router.image_generations


async def patched_image_generations(request, form_data, metadata=None, user=None):
    from open_webui.routers.images import get_image_config
    import uuid

    image_config = await get_image_config()
    engine = image_config.IMAGE_GENERATION_ENGINE

    if engine != "comfyui":
        return await _original_image_generations(
            request, form_data, metadata, user
        )

    # =========================================================================
    # LAYER 1 — Admin defaults
    #
    # These come from the Admin UI: Image Size, Model, Steps.
    # If the LLM omits a parameter, the admin default is used instead.
    # =========================================================================
    log.info("ComfyUI image generation requested — resolving Layer 1 defaults")

    size = "512x512"
    if image_config.IMAGE_SIZE and "x" in image_config.IMAGE_SIZE:
        size = image_config.IMAGE_SIZE
    if form_data.size and "x" in form_data.size:
        size = form_data.size
    width, height = tuple(map(int, size.split("x")))

    # Reduce dimensions to their lowest ratio via GCD
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

    model = await get_image_model(request)
    log.info("Model resolved: %s", model)

    # =========================================================================
    # LAYER 2 — Node bindings
    #
    # The set of parameter types for which the admin has defined a node
    # binding in Workflow Nodes. Parameters without a binding are silently
    # ignored — there is nowhere to inject them.
    # =========================================================================
    from open_webui.routers.images import (
        get_image_data,
        upload_image,
    )
    from open_webui.utils.images.comfyui import (
        ComfyUICreateImageForm,
        ComfyUIWorkflow,
        comfyui_create_image,
    )

    configured_node_types = {
        node.type for node in image_config.COMFYUI_WORKFLOW_NODES
    }
    log.info(
        "Layer 2 — Configured node types: %s",
        sorted(configured_node_types),
    )

    # =========================================================================
    # LAYER 3 — Payload build
    #
    # Every parameter in the payload goes through this check:
    #   1. Is the node type configured in Layer 2?
    #   2. Did the LLM provide a value? If not, fall back to Layer 1.
    #   3. If there is no Layer 1 default either, the parameter is omitted.
    #
    # prompt, width, height and seed are always sent.
    # steps and model are only sent when the LLM explicitly requests them.
    # =========================================================================
    data = {
        "prompt": form_data.prompt,
        "width": reduced_w,
        "height": reduced_h,
        "n": 1,
    }

    # Seed — always sent; defaults to 0 for consistency across calls
    seed = form_data.seed if form_data.seed is not None else 0
    data["seed"] = seed
    if "seed" not in configured_node_types:
        log.warning(
            "Seed=%d was provided but no 'seed' node binding is configured. "
            "The value will be ignored by ComfyUI.",
            seed,
        )

    # Steps — only sent if the LLM explicitly passed a value
    if form_data.steps is not None:
        data["steps"] = form_data.steps
        if "steps" not in configured_node_types:
            log.warning(
                "Steps=%d was provided but no 'steps' node binding is "
                "configured. The value will be ignored by ComfyUI.",
                form_data.steps,
            )
    else:
        log.info("Steps omitted — ComfyUI will use its workflow default")

    # Model — only sent if the LLM explicitly passed a value
    if form_data.model is not None:
        data["model"] = form_data.model
        if "model" not in configured_node_types:
            log.warning(
                "Model='%s' was provided but no 'model' node binding is "
                "configured. The value will be ignored.",
                form_data.model,
            )
    else:
        log.info("Model omitted — using admin default: %s", model)

    log.info(
        "Layer 3 — Payload built: prompt_len=%d, width=%d, height=%d, "
        "seed=%d, steps=%s, model=%s",
        len(data["prompt"]),
        data["width"],
        data["height"],
        data["seed"],
        data.get("steps", "workflow_default"),
        data.get("model", "admin_default"),
    )

    # =========================================================================
    # LAYER 4 — ComfyUI execution
    #
    # The workflow JSON is loaded, node bindings are applied by the patched
    # _apply_workflow_nodes, and the prompt is queued via WebSocket.
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
            model,
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

    metadata = metadata or {}
    images = []
    headers = None
    if image_config.COMFYUI_API_KEY:
        headers = {"Authorization": f"Bearer {image_config.COMFYUI_API_KEY}"}

    for img in res["data"]:
        img_data, ctype = await get_image_data(
            img["url"],
            headers,
            trusted_base_url=image_config.COMFYUI_BASE_URL,
        )
        _, url = await upload_image(
            request,
            img_data,
            ctype,
            {**cf_form.model_dump(exclude_none=True), **metadata},
            user,
        )
        images.append({"url": url})

    log.info(
        "Image generation complete — %d image(s) uploaded", len(images)
    )
    return images


images_router.image_generations = patched_image_generations

log.info(
    "MONKEY PATCH 3: image_generations patched for ComfyUI "
    "with 4-layer hierarchy"
)

# =============================================================================
# TOOL EXPOSED TO THE LLM
#
# This is the function the LLM calls via native tool calling.
# It respects the tool selector (⚙️) in the chat — activate or deactivate
# Image Generator Pro from there. The chip (📷) controls the built-in
# generate_image independently.
# =============================================================================
async def generate_image_pro(
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
    Generate one image with full control over model, seed, size, and steps.

    Works with any engine configured in Open WebUI.
    For ComfyUI:
      - Dimensions are reduced to their lowest ratio (GCD) before sending,
        so "2000x3000" becomes 2×3. Your workflow handles scaling.
      - Seed defaults to 0 for reproducibility. Change it for variation.
      - Steps and model default to the ComfyUI workflow values unless
        explicitly passed.
      - Parameters are only injected if a corresponding node binding
        exists in the workflow nodes configuration.

    Activate this tool from the tool selector (⚙️) in the chat input.
    The image generation chip (📷) controls the built-in generate_image
    independently.

    :param prompt: What to generate
    :param model: Model or checkpoint override. Falls back to admin config
        and then to the workflow default if omitted.
    :param size: Dimensions as "WxH" (e.g., "2000x3000" → 2×3 for ComfyUI).
        Falls back to admin config if omitted.
    :param steps: Inference steps. Falls back to ComfyUI workflow default
        if omitted.
    :param seed: Random seed. Defaults to 0 (reproducible). Same seed +
        same prompt = same image every time. Ignored by OpenAI and Gemini.
    :return: JSON with status and success confirmation
    """
    if __request__ is None:
        log.error(
            "generate_image_pro called without request context"
        )
        return json.dumps(
            {"error": "Request context not available"}
        )

    try:
        from open_webui.models.users import UserModel
        from open_webui.models.chats import Chats

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

        image_files = [
            {"type": "image", "url": img["url"]} for img in images
        ]

        # Persist files to the chat message if context is available
        if __chat_id__ and __message_id__ and images:
            db_files = (
                await Chats.add_message_files_by_id_and_message_id(
                    __chat_id__, __message_id__, image_files
                )
            )
            if db_files is not None:
                image_files = db_files

        # Emit images to the UI via the event emitter
        if __event_emitter__ and image_files:
            await __event_emitter__(
                {
                    "type": "chat:message:files",
                    "data": {"files": image_files},
                }
            )

        log.info(
            "Image delivered to chat — %d file(s)", len(image_files)
        )

        return json.dumps(
            {
                "status": "success",
                "message": (
                    "The image has been successfully generated and is "
                    "already visible to the user in the chat. You do not "
                    "need to display or embed the image again - just "
                    "acknowledge that it has been created."
                ),
                "images": images,
            },
            ensure_ascii=False,
        )

    except Exception as e:
        log.exception("generate_image_pro failed: %s", e)
        return json.dumps({"error": str(e)})

"""
title: Image Generator Pro
author: Abel
version: 1.5
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
        default=None, description="Seed for reproducibility (0 = first image)"
    )


images_router.CreateImageForm = PatchedCreateImageForm
images_router.GenerateImageForm = PatchedCreateImageForm

# =============================================================================
# MONKEY PATCH 2: Make _apply_workflow_nodes ignore None values
#
# The original function always overwrites workflow node inputs even when
# the payload value is None. This causes the workflow to receive null for
# fields like steps when the LLM omits them.
#
# Our patched version skips assignment when the value is None, preserving
# whatever default the exported workflow JSON has.
# =============================================================================
from open_webui.utils.images import comfyui as comfyui_module


def patched_apply_workflow_nodes(workflow, nodes, model, payload):
    """Like the original, but skips assignment when the payload value is None."""
    for node in nodes:
        if node.type:
            if node.type == 'model':
                if model is not None:
                    for node_id in node.node_ids:
                        workflow[node_id]['inputs'][node.key] = model

            elif node.type == 'prompt':
                if payload.prompt is not None:
                    for node_id in node.node_ids:
                        workflow[node_id]['inputs'][node.key if node.key else 'text'] = payload.prompt

            elif node.type == 'width':
                if payload.width is not None:
                    for node_id in node.node_ids:
                        workflow[node_id]['inputs'][node.key if node.key else 'width'] = payload.width

            elif node.type == 'height':
                if payload.height is not None:
                    for node_id in node.node_ids:
                        workflow[node_id]['inputs'][node.key if node.key else 'height'] = payload.height

            elif node.type == 'steps':
                if payload.steps is not None:
                    for node_id in node.node_ids:
                        workflow[node_id]['inputs'][node.key if node.key else 'steps'] = payload.steps

            elif node.type == 'seed':
                if payload.seed is not None:
                    for node_id in node.node_ids:
                        workflow[node_id]['inputs'][node.key] = payload.seed

            elif node.type == 'n':
                if payload.n is not None:
                    for node_id in node.node_ids:
                        workflow[node_id]['inputs'][node.key if node.key else 'batch_size'] = payload.n

            elif node.type == 'image':
                if payload.image is not None:
                    if isinstance(payload.image, list):
                        for idx, node_id in enumerate(node.node_ids):
                            if idx < len(payload.image):
                                workflow[node_id]['inputs'][node.key] = payload.image[idx]
                    else:
                        for node_id in node.node_ids:
                            workflow[node_id]['inputs'][node.key] = payload.image

            else:
                if node.value is not None:
                    for node_id in node.node_ids:
                        workflow[node_id]['inputs'][node.key] = node.value


comfyui_module._apply_workflow_nodes = patched_apply_workflow_nodes

# =============================================================================
# MONKEY PATCH 3: Intercept image_generations for ComfyUI
# =============================================================================
_original_image_generations = images_router.image_generations


async def patched_image_generations(request, form_data, metadata=None, user=None):
    from open_webui.routers.images import get_image_config
    import uuid

    image_config = await get_image_config()
    engine = image_config.IMAGE_GENERATION_ENGINE

    if engine == "comfyui":
        from open_webui.routers.images import (
            get_image_model,
            get_image_data,
            upload_image,
        )
        from open_webui.utils.images.comfyui import (
            ComfyUICreateImageForm,
            ComfyUIWorkflow,
            comfyui_create_image,
        )

        # Resolve size: user parameter > admin config > 512x512 fallback
        size = "512x512"
        if image_config.IMAGE_SIZE and "x" in image_config.IMAGE_SIZE:
            size = image_config.IMAGE_SIZE
        if form_data.size and "x" in form_data.size:
            size = form_data.size
        width, height = tuple(map(int, size.split("x")))

        # Reduce dimensions to their lowest ratio via GCD
        gcd = math.gcd(width, height)
        width //= gcd
        height //= gcd

        metadata = metadata or {}
        model = await get_image_model(request)

        # Build payload — seed is always sent (defaults to 0 for reproducibility).
        # Steps and model are only included when the LLM explicitly provides them.
        data = {
            "prompt": form_data.prompt,
            "width": width,
            "height": height,
            "n": 1,
        }

        # Seed is always sent; defaults to 0 if the LLM doesn't specify one
        seed = form_data.seed if form_data.seed is not None else 0
        data["seed"] = seed

        if form_data.steps is not None:
            data["steps"] = form_data.steps
        if form_data.model is not None:
            data["model"] = form_data.model

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

        res = await comfyui_create_image(
            model,
            cf_form,
            str(uuid.uuid4()),
            image_config.COMFYUI_BASE_URL,
            image_config.COMFYUI_API_KEY,
        )

        images = []
        headers = None
        if image_config.COMFYUI_API_KEY:
            headers = {"Authorization": f"Bearer {image_config.COMFYUI_API_KEY}"}
        for img in res["data"]:
            img_data, ctype = await get_image_data(
                img["url"], headers, trusted_base_url=image_config.COMFYUI_BASE_URL
            )
            _, url = await upload_image(
                request,
                img_data,
                ctype,
                {**cf_form.model_dump(exclude_none=True), **metadata},
                user,
            )
            images.append({"url": url})
        return images

    # For any other engine, delegate to the original function unchanged
    return await _original_image_generations(request, form_data, metadata, user)


images_router.image_generations = patched_image_generations

# =============================================================================
# TOOL EXPOSED TO THE LLM
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
    Generate one image with control over model, seed, size, and steps.

    Works with any engine configured in Open WebUI.
    For ComfyUI:
      - Dimensions are reduced to their lowest ratio (GCD) before sending.
      - Seed defaults to 0 for reproducibility. Change it for variation.
      - Steps and model default to the ComfyUI workflow values unless passed.
      - Seed requires a "seed" node configured in your workflow nodes.

    Activate this tool from the tool selector (⚙️) in the chat input.
    The image generation chip (📷) controls the built-in generate_image
    independently.

    :param prompt: What to generate
    :param model: Model/checkpoint override. Falls back to admin config if omitted.
    :param size: Dimensions as "WxH" (e.g., "2000x3000" becomes 2x3 for ComfyUI).
        Falls back to admin config if omitted.
    :param steps: Inference steps. Falls back to ComfyUI workflow default if omitted.
    :param seed: Random seed. Defaults to 0 (reproducible). Same seed + same prompt =
        same image every time. Ignored by OpenAI/Gemini.
    :return: JSON with status and success confirmation
    """
    if __request__ is None:
        return json.dumps({"error": "Request context not available"})

    try:
        from open_webui.models.users import UserModel
        from open_webui.models.chats import Chats

        user = UserModel(**__user__) if __user__ else None

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

        image_files = [{"type": "image", "url": img["url"]} for img in images]

        if __chat_id__ and __message_id__ and images:
            db_files = await Chats.add_message_files_by_id_and_message_id(
                __chat_id__, __message_id__, image_files
            )
            if db_files is not None:
                image_files = db_files

        if __event_emitter__ and image_files:
            await __event_emitter__(
                {
                    "type": "chat:message:files",
                    "data": {"files": image_files},
                }
            )

        return json.dumps(
            {
                "status": "success",
                "message": "Image generated.",
                "seed_used": seed,
            },
            ensure_ascii=False,
        )

    except Exception as e:
        log.exception(f"generate_image_pro error: {e}")
        return json.dumps({"error": str(e)})

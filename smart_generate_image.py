"""
title: Image Generator Pro
author: Abel
version: 1.2
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
        default=None, description="Seed for reproducibility (-1 = random)"
    )


images_router.CreateImageForm = PatchedCreateImageForm
images_router.GenerateImageForm = PatchedCreateImageForm

# =============================================================================
# MONKEY PATCH 2: Intercept image_generations only for ComfyUI
# =============================================================================
_original_image_generations = images_router.image_generations


async def patched_image_generations(request, form_data, metadata=None, user=None):
    from open_webui.routers.images import get_image_config
    import uuid

    image_config = await get_image_config()
    engine = image_config.IMAGE_GENERATION_ENGINE
    seed = getattr(form_data, "seed", None)

    # Only intercept ComfyUI + seed to inject extra params
    if engine == "comfyui" and seed is not None:
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

        # Resolve size: user param > admin config > 512x512 fallback
        size = "512x512"
        if image_config.IMAGE_SIZE and "x" in image_config.IMAGE_SIZE:
            size = image_config.IMAGE_SIZE
        if form_data.size and "x" in form_data.size:
            size = form_data.size
        width, height = tuple(map(int, size.split("x")))

        # 🔻 Reduce dimensions to their lowest ratio via GCD
        gcd = math.gcd(width, height)
        width //= gcd
        height //= gcd

        metadata = metadata or {}
        model = await get_image_model(request)

        # Build payload — only include what the user explicitly provides
        data = {
            "prompt": form_data.prompt,
            "width": width,
            "height": height,
            "n": 1,
            "seed": seed,
        }

        if form_data.steps is not None:
            data["steps"] = form_data.steps
        if form_data.negative_prompt is not None:
            data["negative_prompt"] = form_data.negative_prompt
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

    # For any other engine (or ComfyUI without seed), delegate to original
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
    seed: int | None = None,
    negative_prompt: str | None = None,
    __request__=None,
    __user__=None,
    __event_emitter__=None,
    __chat_id__=None,
    __message_id__=None,
):
    """
    Generate one image with full control over model, seed, size, steps,
    and negative prompt. Respects admin-configured defaults.

    Works with any engine configured in Open WebUI.
    ComfyUI-specific: seed is fully supported; dimensions are reduced to
    their lowest ratio (GCD) before being sent to the workflow.
    Steps and negative_prompt default to whatever the ComfyUI workflow
    defines — they are only overridden when explicitly provided.

    :param prompt: What to generate
    :param model: Model/checkpoint override. Falls back to admin config if omitted.
    :param size: Dimensions as "WxH" (e.g., "2000x3000" → 2x3 for ComfyUI).
        Falls back to admin config if omitted.
    :param steps: Inference steps. Overrides ComfyUI workflow default if set.
    :param seed: Random seed for reproducibility. Same seed + same prompt =
        same image every time. Works with ComfyUI (requires a "seed" node
        configured in workflow nodes). Ignored by OpenAI/Gemini.
    :param negative_prompt: What to avoid. Overrides ComfyUI workflow default if set.
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
                negative_prompt=negative_prompt,
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
                "seed_used": seed if seed is not None else -1,
            },
            ensure_ascii=False,
        )

    except Exception as e:
        log.exception(f"generate_image_pro error: {e}")
        return json.dumps({"error": str(e)})

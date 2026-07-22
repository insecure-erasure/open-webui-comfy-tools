"""
title: Image Generator Pro
author: Abel
version: 1.1
"""

import json
import logging
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# =============================================================================
# ÚNICO MONKEY PATCH: Añadir seed a CreateImageForm
# =============================================================================
from open_webui.routers import images as images_router

class PatchedCreateImageForm(images_router.CreateImageForm):
    seed: int | None = Field(default=None, description="Seed for reproducibility")

images_router.CreateImageForm = PatchedCreateImageForm
images_router.GenerateImageForm = PatchedCreateImageForm

# =============================================================================
# WRAPER AGNÓSTICO de image_generations
# Solo propaga seed si el form lo trae, sin saber qué engine es
# =============================================================================
_original_image_generations = images_router.image_generations

async def patched_image_generations(request, form_data, metadata=None, user=None):
    """Toma seed del form y lo inyecta en metadata para que los engines lo recojan."""
    metadata = metadata or {}
    seed = getattr(form_data, 'seed', None)
    if seed is not None:
        metadata['seed'] = seed
    return await _original_image_generations(request, form_data, metadata, user)

images_router.image_generations = patched_image_generations

# =============================================================================
# HERRAMIENTA
# =============================================================================
async def generate_image_pro(
    prompt: str,
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
    Generate one image with optional seed, size, steps, and negative prompt.
    Works with any image generation engine configured in Open WebUI.

    :param prompt: What to generate
    :param size: Dimensions as "WxH" (e.g., "1024x768"). Falls back to admin config.
    :param steps: Inference steps. Falls back to admin config.
    :param seed: Random seed for reproducibility. Same seed + same prompt = same image.
        Leave empty or -1 for random. Requires engine support (ComfyUI, A1111).
        Ignored by engines that don't support it (OpenAI, Gemini).
    :param negative_prompt: What to avoid in the image
    :return: Success confirmation.
    """
    if __request__ is None:
        return json.dumps({'error': 'Request context not available'})

    try:
        from open_webui.models.users import UserModel
        from open_webui.models.chats import Chats

        user = UserModel(**__user__) if __user__ else None

        images = await patched_image_generations(
            request=__request__,
            form_data=PatchedCreateImageForm(
                prompt=prompt,
                size=size,
                steps=steps,
                seed=seed,
                negative_prompt=negative_prompt,
            ),
            user=user,
        )

        image_files = [{'type': 'image', 'url': img['url']} for img in images]

        if __chat_id__ and __message_id__ and images:
            db_files = await Chats.add_message_files_by_id_and_message_id(
                __chat_id__, __message_id__, image_files,
            )
            if db_files is not None:
                image_files = db_files

        if __event_emitter__ and image_files:
            await __event_emitter__({
                'type': 'chat:message:files',
                'data': {'files': image_files},
            })

        return json.dumps({
            'status': 'success',
            'message': 'Image generated.',
            'seed_used': seed if seed is not None else -1,
        }, ensure_ascii=False)

    except Exception as e:
        log.exception(f'generate_image_pro error: {e}')
        return json.dumps({'error': str(e)})

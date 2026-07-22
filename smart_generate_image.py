"""
title: Image Generator Pro
author: Abel
version: 1.0
"""

import json
import logging

log = logging.getLogger(__name__)

# =============================================================================
# 1. MONKEY PATCH: seed en CreateImageForm
# =============================================================================
from open_webui.routers import images as images_router
from pydantic import BaseModel, Field

class PatchedCreateImageForm(images_router.CreateImageForm):
    seed: int | None = Field(default=None, description="Seed for reproducibility")

images_router.CreateImageForm = PatchedCreateImageForm
images_router.GenerateImageForm = PatchedCreateImageForm

# =============================================================================
# 2. MONKEY PATCH: image_generations con seed para ComfyUI
# =============================================================================
_original_image_generations = images_router.image_generations

async def patched_image_generations(request, form_data, metadata=None, user=None):
    from open_webui.routers.images import (
        get_image_config, get_image_model, get_image_data, upload_image,
    )
    from open_webui.utils.images.comfyui import (
        ComfyUICreateImageForm, ComfyUIWorkflow, comfyui_create_image,
    )
    import uuid

    image_config = await get_image_config()
    seed = getattr(form_data, 'seed', None)

    # Tamaño
    size = '512x512'
    if image_config.IMAGE_SIZE and 'x' in image_config.IMAGE_SIZE:
        size = image_config.IMAGE_SIZE
    if form_data.size and 'x' in form_data.size:
        size = form_data.size
    width, height = tuple(map(int, size.split('x')))

    metadata = metadata or {}
    model = await get_image_model(request)

    try:
        # --- ENGINE: ComfyUI (con seed) ---
        if image_config.IMAGE_GENERATION_ENGINE == 'comfyui':
            data = {
                'prompt': form_data.prompt,
                'width': width,
                'height': height,
                'n': 1,
            }
            if image_config.IMAGE_STEPS is not None or form_data.steps is not None:
                data['steps'] = form_data.steps if form_data.steps is not None else image_config.IMAGE_STEPS
            if form_data.negative_prompt is not None:
                data['negative_prompt'] = form_data.negative_prompt
            if seed is not None:
                data['seed'] = seed

            cf_form = ComfyUICreateImageForm(**{
                'workflow': ComfyUIWorkflow(**{
                    'workflow': image_config.COMFYUI_WORKFLOW,
                    'nodes': image_config.COMFYUI_WORKFLOW_NODES,
                }),
                **data,
            })
            res = await comfyui_create_image(
                model, cf_form, str(uuid.uuid4()),
                image_config.COMFYUI_BASE_URL, image_config.COMFYUI_API_KEY,
            )

            images = []
            headers = None
            if image_config.COMFYUI_API_KEY:
                headers = {'Authorization': f'Bearer {image_config.COMFYUI_API_KEY}'}
            for img in res['data']:
                img_data, ctype = await get_image_data(
                    img['url'], headers,
                    trusted_base_url=image_config.COMFYUI_BASE_URL,
                )
                _, url = await upload_image(request, img_data, ctype,
                    {**cf_form.model_dump(exclude_none=True), **metadata}, user)
                images.append({'url': url})
            return images

        # --- ENGINE: OpenAI / Gemini (seed se ignora) ---
        else:
            return await _original_image_generations(request, form_data, metadata, user)

    except Exception as e:
        log.exception(f'patched_image_generations error: {e}')
        raise

images_router.image_generations = patched_image_generations

# =============================================================================
# 3. HERRAMIENTA
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
    Works with ComfyUI (seed requires a "seed" node in your workflow config),
    OpenAI DALL-E and Google Gemini (seed is ignored).

    :param prompt: What to generate
    :param size: Dimensions as "WxH" (e.g., "1024x768"). Falls back to admin config.
    :param steps: Inference steps. Falls back to admin config.
    :param seed: Random seed. Same seed + same prompt = same image every time.
        Leave empty or use -1 for random. Works with ComfyUI if you have a
        "seed" node configured in the admin workflow nodes.
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

"""
title: Image Generator Pro
author: Abel
version: 1.2
"""

import json
import logging
import random

log = logging.getLogger(__name__)

# =============================================================================
# 1. MONKEY PATCH: Añadir seed a CreateImageForm
# =============================================================================
from open_webui.routers import images as images_router
from pydantic import BaseModel, Field

class PatchedCreateImageForm(images_router.CreateImageForm):
    seed: int | None = Field(default=None, description="Seed for reproducibility")

# Reemplazamos la clase en el módulo
images_router.CreateImageForm = PatchedCreateImageForm
images_router.GenerateImageForm = PatchedCreateImageForm

# =============================================================================
# 2. MONKEY PATCH: image_generations para transferir seed a cada engine
# =============================================================================
_original_image_generations = images_router.image_generations

async def patched_image_generations(request, form_data, metadata=None, user=None):
    """Inyecta seed en el payload antes de cada engine."""
    from open_webui.routers.images import (
        get_image_config, get_image_model, get_image_data, upload_image,
        get_automatic1111_api_auth
    )
    from open_webui.utils.images.comfyui import (
        ComfyUICreateImageForm, ComfyUIWorkflow, comfyui_create_image
    )
    from open_webui.utils.session_pool import get_session
    from open_webui.env import AIOHTTP_CLIENT_SESSION_SSL
    import aiohttp, uuid, re

    image_config = await get_image_config()
    seed = getattr(form_data, 'seed', None)

    # Resolución de tamaño
    size = '512x512'
    if image_config.IMAGE_SIZE and 'x' in image_config.IMAGE_SIZE:
        size = image_config.IMAGE_SIZE
    if form_data.size and 'x' in form_data.size:
        size = form_data.size
    width, height = tuple(map(int, size.split('x')))

    metadata = metadata or {}
    model = await get_image_model(request)

    try:
        # --- ENGINE: AUTOMATIC1111 ---
        if image_config.IMAGE_GENERATION_ENGINE in ['', 'automatic1111']:
            if form_data.model:
                await images_router.set_image_model(request, form_data.model)

            data = {
                'prompt': form_data.prompt,
                'batch_size': form_data.n,
                'width': width,
                'height': height,
                'seed': seed if seed is not None else -1,
            }
            if image_config.IMAGE_STEPS is not None or form_data.steps is not None:
                data['steps'] = form_data.steps if form_data.steps is not None else image_config.IMAGE_STEPS
            if form_data.negative_prompt is not None:
                data['negative_prompt'] = form_data.negative_prompt
            if image_config.AUTOMATIC1111_PARAMS:
                data = {**data, **image_config.AUTOMATIC1111_PARAMS}

            session = await get_session()
            async with session.post(
                f'{image_config.AUTOMATIC1111_BASE_URL}/sdapi/v1/txt2img',
                json=data,
                headers={'authorization': get_automatic1111_api_auth(image_config)},
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as r:
                res = await r.json(content_type=None)

            images = []
            for img in res['images']:
                img_data, ctype = await get_image_data(img)
                _, url = await upload_image(request, img_data, ctype,
                    {**data, 'info': res['info'], **metadata}, user)
                images.append({'url': url})
            return images

        # --- ENGINE: ComfyUI ---
        elif image_config.IMAGE_GENERATION_ENGINE == 'comfyui':
            data = {
                'prompt': form_data.prompt,
                'width': width,
                'height': height,
                'n': 1,  # siempre 1
            }
            if image_config.IMAGE_STEPS is not None or form_data.steps is not None:
                data['steps'] = form_data.steps if form_data.steps is not None else image_config.IMAGE_STEPS
            if form_data.negative_prompt is not None:
                data['negative_prompt'] = form_data.negative_prompt
            if seed is not None:
                data['seed'] = seed  # ← se lo pasamos a ComfyUICreateImageForm

            cf_form = ComfyUICreateImageForm(**{
                'workflow': ComfyUIWorkflow(**{
                    'workflow': image_config.COMFYUI_WORKFLOW,
                    'nodes': image_config.COMFYUI_WORKFLOW_NODES,
                }),
                **data,
            })
            res = await comfyui_create_image(model, cf_form, str(uuid.uuid4()),
                image_config.COMFYUI_BASE_URL, image_config.COMFYUI_API_KEY)

            images = []
            headers = None
            if image_config.COMFYUI_API_KEY:
                headers = {'Authorization': f'Bearer {image_config.COMFYUI_API_KEY}'}
            for img in res['data']:
                img_data, ctype = await get_image_data(img['url'], headers,
                    trusted_base_url=image_config.COMFYUI_BASE_URL)
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
# 3. HERRAMIENTA EXPUESTA AL LLM (AGNÓSTICA)
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
    Agnostic — works with ComfyUI, AUTOMATIC1111, OpenAI, and Gemini.

    :param prompt: What to generate
    :param size: Dimensions as "WxH" (e.g., "1024x768"). Falls back to admin config.
    :param steps: Inference steps. Falls back to admin config.
    :param seed: Random seed. Same seed + same prompt = same image. -1 = random.
        Works with ComfyUI (requires a "seed" node in workflow config) and A1111.
        Ignored by OpenAI/Gemini.
    :param negative_prompt: What to avoid.
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

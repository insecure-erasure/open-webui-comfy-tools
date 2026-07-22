"""
title: Advanced Image Generator
author: Abel
version: 1.0
"""

import json
import logging
import uuid
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# =============================================================================
# MONKEY PATCH: Añadir seed a CreateImageForm
# =============================================================================
from open_webui.routers import images as images_router

# Parcheamos la clase CreateImageForm para que tenga seed
original_create_form = images_router.CreateImageForm

class PatchedCreateImageForm(original_create_form):  # ← subclase, hereda todo
    seed: int | None = Field(default=None, description="Seed for reproducibility (-1 = random)")

# Reemplazamos en el módulo para que image_generations lo vea
images_router.CreateImageForm = PatchedCreateImageForm
# También el alias
images_router.GenerateImageForm = PatchedCreateImageForm

# =============================================================================
# MONKEY PATCH: image_generations para que use seed
# =============================================================================
_original_image_generations = images_router.image_generations

async def patched_image_generations(request, form_data, metadata=None, user=None):
    """Wrapper que inyecta seed en cada engine antes de llamar al original."""
    
    # Extraemos el seed del form_data antes de que se pierda
    seed = getattr(form_data, 'seed', None)
    
    # Llamamos al original (que internamente despacha a cada engine)
    result = await _original_image_generations(request, form_data, metadata, user)
    
    return result

# Parcheamos image_generations - PERO necesitamos modificar los branches internos
# Mejor: parcheamos directamente el flujo de cada engine desde aquí
# En realidad, hagamos nuestra propia versión completa:

async def patched_image_generations(request, form_data, metadata=None, user=None):
    """Versión con soporte de seed para A1111 y ComfyUI."""
    from open_webui.routers.images import (
        get_image_config, get_image_model, get_image_data, upload_image
    )
    from open_webui.config import IMAGE_URL_RESPONSE_MODELS_REGEX_PATTERN
    from open_webui.utils.images.comfyui import (
        ComfyUICreateImageForm, ComfyUIWorkflow, comfyui_create_image
    )
    from open_webui.env import (
        AIOHTTP_CLIENT_SESSION_SSL, ENABLE_FORWARD_USER_INFO_HEADERS
    )
    from open_webui.utils.headers import include_user_info_headers
    from open_webui.utils.session_pool import get_session
    from open_webui.utils.images.comfyui import comfyui_create_image
    import re
    import aiohttp
    
    image_config = await get_image_config()
    
    # Resolución de tamaño
    size = '512x512'
    if image_config.IMAGE_SIZE and 'x' in image_config.IMAGE_SIZE:
        size = image_config.IMAGE_SIZE
    if form_data.size and 'x' in form_data.size:
        size = form_data.size
    width, height = tuple(map(int, size.split('x')))
    
    metadata = metadata or {}
    model = await get_image_model(request)
    seed = getattr(form_data, 'seed', None)
    
    try:
        # --- ENGINE: AUTOMATIC1111 ---
        if image_config.IMAGE_GENERATION_ENGINE in ['', 'automatic1111']:
            if form_data.model:
                await images_router.set_image_model(request, form_data.model)

            # 🔥 SEED INCLUIDO AQUÍ
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

            from open_webui.routers.images import get_automatic1111_api_auth
            session = await get_session()
            async with session.post(
                url=f'{image_config.AUTOMATIC1111_BASE_URL}/sdapi/v1/txt2img',
                json=data,
                headers={'authorization': get_automatic1111_api_auth(image_config)},
                ssl=AIOHTTP_CLIENT_SESSION_SSL,
            ) as r:
                res = await r.json(content_type=None)

            images = []
            for image in res['images']:
                image_data, content_type = await get_image_data(image)
                _, url = await upload_image(request, image_data, content_type,
                                            {**data, 'info': res['info'], **metadata}, user)
                images.append({'url': url})
            return images

        # --- ENGINE: ComfyUI ---
        elif image_config.IMAGE_GENERATION_ENGINE == 'comfyui':
            data = {
                'prompt': form_data.prompt,
                'width': width,
                'height': height,
                'n': form_data.n,
            }
            if image_config.IMAGE_STEPS is not None or form_data.steps is not None:
                data['steps'] = form_data.steps if form_data.steps is not None else image_config.IMAGE_STEPS
            if form_data.negative_prompt is not None:
                data['negative_prompt'] = form_data.negative_prompt
            # 🔥 SEED para ComfyUI
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
                image_data, content_type = await get_image_data(
                    img['url'], headers,
                    trusted_base_url=image_config.COMFYUI_BASE_URL,
                )
                _, url = await upload_image(request, image_data, content_type,
                                            {**cf_form.model_dump(exclude_none=True), **metadata}, user)
                images.append({'url': url})
            return images

        # --- ENGINE: OpenAI / Gemini (seed se ignora) ---
        else:
            return await _original_image_generations(request, form_data, metadata, user)

    except Exception as e:
        error = e
        if isinstance(e, aiohttp.ClientResponseError):
            error = e.message
        log.exception(f'patched_image_generations error: {e}')
        raise

# Aplicamos el parche
images_router.image_generations = patched_image_generations

# =============================================================================
# HERRAMIENTA EXPUESTA AL LLM
# =============================================================================
async def generate_image_advanced(
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
    Generate an image with full control over seed, size, steps, and negative prompt.

    :param prompt: Detailed description of the image to generate
    :param size: Image dimensions as "WxH" (e.g., "512x512", "1024x768"). Falls back to admin default if omitted.
    :param steps: Number of inference steps. Higher = more detail. Falls back to admin default if omitted.
    :param seed: Random seed for reproducibility. Use -1 for random. Same seed + same prompt = same image.
    :param negative_prompt: What to avoid in the generated image
    :return: Confirmation that the image was generated
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
            'message': 'Image generated successfully.',
            'seed_used': seed if seed is not None else -1,
        }, ensure_ascii=False)

    except Exception as e:
        log.exception(f'generate_image_advanced error: {e}')
        return json.dumps({'error': str(e)})

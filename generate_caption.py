"""
title: Generate Caption
author: Insecure Erasure
description: Generate a detailed caption for an image using Florence-2 via ComfyUI
version: 2.0
"""

import asyncio
import json
import logging
import random as _random
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# =============================================================================
# ComfyUI constants
# =============================================================================
_COMFY_QUEUE_MAX_RETRIES = 120       # ~2 min at 1s intervals
_COMFY_QUEUE_POLL_INTERVAL = 1.0     # seconds

# =============================================================================
# Model options
# =============================================================================
_MODEL_OPTIONS = [
    {"value": "", "label": "System default"},
    {"value": "Florence-2-base-ft", "label": "Florence-2-base-ft"},
    {"value": "Florence-2-Flux-Large", "label": "Florence-2-Flux-Large"},
    {"value": "Florence-2-large-interleaved", "label": "Florence-2-large-interleaved"},
    {"value": "Florence-2-large-nsfw-pt", "label": "Florence-2-large-nsfw-pt"},
]

_USER_MODEL_OPTIONS = [
    {"value": "Florence-2-base-ft", "label": "Florence-2-base-ft"},
    {"value": "Florence-2-Flux-Large", "label": "Florence-2-Flux-Large"},
    {"value": "Florence-2-large-interleaved", "label": "Florence-2-large-interleaved"},
    {"value": "Florence-2-large-nsfw-pt", "label": "Florence-2-large-nsfw-pt"},
]

_DEFAULT_MODEL = "Florence-2-base-ft"

_TASK_OPTIONS = [
    {"value": "", "label": "System default"},
    {"value": "caption", "label": "caption"},
    {"value": "detailed_caption", "label": "detailed_caption"},
    {"value": "more_detailed_caption", "label": "more_detailed_caption"},
    {"value": "nsfw_caption", "label": "nsfw_caption"},
    {"value": "nsfw_detailed_caption", "label": "nsfw_detailed_caption"},
]

_USER_TASK_OPTIONS = [
    {"value": "caption", "label": "caption"},
    {"value": "detailed_caption", "label": "detailed_caption"},
    {"value": "more_detailed_caption", "label": "more_detailed_caption"},
    {"value": "nsfw_caption", "label": "nsfw_caption"},
    {"value": "nsfw_detailed_caption", "label": "nsfw_detailed_caption"},
]

_DEFAULT_TASK = "detailed_caption"

# =============================================================================
# Token/beam configuration
# =============================================================================
_DEFAULT_MAX_NEW_TOKENS = 1024
_DEFAULT_NUM_BEAMS = 4
_COMFY_SEED_MAX: int = 1125899906842624

_BEAM_OPTIONS = [
    {"value": str(i), "label": str(i)}
    for i in range(1, 11)
]

_MAX_TOKENS_OPTIONS = [
    {"value": "0", "label": "User decides"},
    {"value": "-1", "label": "Model default (1024)"},
] + [
    {"value": str(i), "label": str(i)}
    for i in [128, 256, 512, 768, 1024, 1536, 2048, 3072, 4096]
]


# =============================================================================
# Workflow node resolver — finds nodes by _meta.title (must be unique)
# =============================================================================

def _resolve_node(workflow: dict, title: str) -> tuple[str, dict]:
    """
    Find a workflow node by its _meta.title.

    Returns (node_id, node_dict). Titles must be unique in the workflow.
    """
    for node_id, node in workflow.items():
        if node.get("_meta", {}).get("title") == title:
            return (node_id, node)
    raise KeyError(
        f"Node with title {title!r} not found in workflow. "
        "Available titles: "
        + ", ".join(
            n.get("_meta", {}).get("title", "(no title)")
            for n in workflow.values()
        )
    )


# =============================================================================
# Workflow loader — cache/tools/<tool_id>/filename.json
# =============================================================================

def _load_workflow(tool_id: str, filename: str) -> str:
    """
    Load the workflow JSON from the tool's cache directory.

    Resolves CACHE_DIR / 'tools' / <tool_id> / <filename>.
    Returns the raw JSON string, ready for json.loads() followed by
    _resolve_node().

    Raises RuntimeError if the tool_id is empty or the file is not found.
    """
    if not tool_id:
        raise RuntimeError(
            "No tool_id provided. The tool must run inside Open WebUI "
            "to resolve the workflow from cache."
        )

    from open_webui.config import CACHE_DIR

    workflow_path = CACHE_DIR / 'tools' / tool_id / filename

    if not workflow_path.exists():
        raise FileNotFoundError(
            f"Workflow file not found at {workflow_path}. "
            f"Copy workflows/{filename} to that path."
        )

    log.info("Loading workflow from %s", workflow_path)
    return workflow_path.read_text(encoding='utf-8')


# =============================================================================
# ComfyUI API helpers
# =============================================================================

async def _comfyui_queue_prompt(
    client: httpx.AsyncClient, base_url: str, api_key: str, workflow: dict
) -> str:
    """Submit a workflow to ComfyUI and return the prompt_id."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "prompt": workflow,
        "client_id": str(uuid.uuid4()),
    }

    resp = await client.post(
        f"{base_url.rstrip('/')}/prompt",
        json=payload,
        headers=headers,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return a prompt_id: {data}")
    return prompt_id


async def _comfyui_wait_for_output(
    client: httpx.AsyncClient, base_url: str, api_key: str, prompt_id: str
) -> dict:
    """Poll /history/{prompt_id} until the workflow completes. Returns the full history entry."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    history_url = f"{base_url.rstrip('/')}/history/{prompt_id}"

    for attempt in range(_COMFY_QUEUE_MAX_RETRIES):
        resp = await client.get(history_url, headers=headers, timeout=10)
        resp.raise_for_status()
        history = resp.json()

        if prompt_id in history and history[prompt_id].get("outputs"):
            return history[prompt_id]

        await asyncio.sleep(_COMFY_QUEUE_POLL_INTERVAL)

    raise TimeoutError(
        f"ComfyUI did not finish within {_COMFY_QUEUE_MAX_RETRIES} seconds "
        f"(prompt_id={prompt_id})"
    )


async def _comfyui_interrupt(base_url: str, api_key: str) -> None:
    """Interrupt the currently running ComfyUI generation."""
    try:
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{base_url.rstrip('/')}/interrupt",
                headers=headers,
                timeout=5,
            )
    except Exception:
        log.warning("Failed to interrupt ComfyUI", exc_info=True)


def _extract_caption(outputs: dict, output_node_id: str) -> str:
    """
    Extract the caption text from the workflow outputs.

    The ShowText|pysssss node exposes the caption text as a single-element
    list under the "text" key: {"text": ["The caption..."]}.

    Returns the caption string.
    """
    node_output = outputs.get(output_node_id, {})

    # ShowText|pysssss stores the text under "text" as ["caption"]
    text_list = node_output.get("text")
    if isinstance(text_list, list) and len(text_list) > 0 and isinstance(text_list[0], str):
        return text_list[0].strip()

    # Fallback: raw string under "text"
    caption = node_output.get("text")
    if isinstance(caption, str):
        return caption.strip()

    # Fallback: raw string under "string"
    caption = node_output.get("string")
    if isinstance(caption, str):
        return caption.strip()

    # Worst case: dump the first string value found
    for key, value in node_output.items():
        if isinstance(value, str) and len(value) > 10:
            return value.strip()
        if isinstance(value, list) and len(value) > 0 and isinstance(value[0], str) and len(value[0]) > 10:
            return value[0].strip()

    raise RuntimeError(
        f"Could not extract caption from output node {output_node_id}. "
        f"Available outputs: {json.dumps(node_output, indent=2)}"
    )


# =============================================================================
# TOOL
# =============================================================================

class Tools:
    """
    Generate a caption / description of an image (in English).

    Only call when the user asks what an image depicts. Also use this
    before editing or enhancing an image to give yourself visual context —
    you cannot see the image directly. Pass an image filename or an URL.

    The returned caption is always in English regardless of the image
    content or the user's language.

    image: The filename previously generated from the smart_generate_image
        response, or a direct URL to an external image.
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        model: str = Field(
            default=_DEFAULT_MODEL,
            description=f"Default Florence-2 model. Users can override this from their valves. Default: {_DEFAULT_MODEL}.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [o for o in _MODEL_OPTIONS if o["value"] != ""],
                }
            },
        )
        task: str = Field(
            default=_DEFAULT_TASK,
            description=f"Default caption task. Users can override this from their valves. Default: {_DEFAULT_TASK}.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": [o for o in _TASK_OPTIONS if o["value"] != ""],
                }
            },
        )
        max_new_tokens: str = Field(
            default="0",
            description="Max new tokens policy. 0 = user decides (no clamp). -1 = force model default (1024). 128-4096 = clamp user value to this ceiling.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _MAX_TOKENS_OPTIONS,
                }
            },
        )
        max_num_beams: str = Field(
            default="0",
            description="Max beam ceiling. 0 = no ceiling (use user value). 1-10 = clamp user value to this ceiling.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _BEAM_OPTIONS,
                }
            },
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        model: str = Field(
            default="",
            description="Override the admin valve model. Leave unselected to use the admin valve default.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _USER_MODEL_OPTIONS,
                }
            },
        )
        task: str = Field(
            default="",
            description="Override the admin valve task. Leave unselected to use the admin valve default.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _USER_TASK_OPTIONS,
                }
            },
        )
        new_tokens: int = Field(
            default=0,
            description="New tokens. 0 = use system default / admin policy. Subject to admin ceiling.",
        )
        num_beams: str = Field(
            default="0",
            description="Number of beams. 0 = use system default. 1-10 = explicit value (subject to admin ceiling).",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _BEAM_OPTIONS,
                }
            },
        )
        do_sample: bool = Field(
            default=False,
            description="Enable sampling (do_sample). False = greedy decoding, True = sampled decoding.",
        )
        seed: int = Field(
            default=-1,
            description="Seed. -1 = random, >=0 = fixed seed for reproducibility.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def generate_caption(
        self,
        image: str,
        detail_level: str = "detailed",
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __id__: str = "",
    ):
        """
        Generate a caption / description of an image.

        Only call when the user asks what an image depicts. Pass an image
        filename or an URL.

        :param image: Filename or URL of the image to caption.
        :param detail_level: How detailed the caption should be.
            "simple" - brief one-line description (use for casual questions like "what's in this image?")
            "detailed" - thorough description with multiple sentences (default, recommended)
            "verbose" - extremely detailed, exhaustive description (use when the user asks for great detail)
        """
        if __request__ is None:
            log.error("generate_caption called without request context")
            return "Error: The tool could not be initialized."

        try:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\U0001f5bc\ufe0f Generating caption...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            from open_webui.routers.images import get_image_config

            image_config = await get_image_config()

            user_valves = (__user__ or {}).get('valves', None)

            # =================================================================
            # Resolve model: UserValves > AdminValves > built-in default
            # =================================================================
            resolved_model = (
                user_valves.model if user_valves and user_valves.model
                else self.valves.model or _DEFAULT_MODEL
            )

            # =================================================================
            # Resolve task: UserValves > AdminValves > built-in default
            # =================================================================
            resolved_task = (
                user_valves.task if user_valves and user_valves.task
                else self.valves.task or _DEFAULT_TASK
            )

            # =================================================================
            # Resolve max_new_tokens with ceiling policy
            # =================================================================
            #   max_new_tokens = 0  → user decides (no clamp)
            #   max_new_tokens = -1 → force model default (ignore user)
            #   max_new_tokens > 0  → clamp user value to this ceiling
            raw_admin_tokens = self.valves.max_new_tokens
            if raw_admin_tokens == "-1":
                admin_max_tokens = -1
            elif raw_admin_tokens == "0" or not raw_admin_tokens:
                admin_max_tokens = 0
            else:
                admin_max_tokens = int(raw_admin_tokens)

            def _get_user_tokens():
                if user_valves and user_valves.new_tokens and user_valves.new_tokens > 0:
                    return user_valves.new_tokens
                return 0

            if admin_max_tokens == -1:
                resolved_tokens = _DEFAULT_MAX_NEW_TOKENS
            elif admin_max_tokens == 0:
                user_tokens = _get_user_tokens()
                resolved_tokens = user_tokens if user_tokens > 0 else _DEFAULT_MAX_NEW_TOKENS
            else:
                user_tokens = _get_user_tokens()
                if user_tokens > 0:
                    resolved_tokens = min(user_tokens, admin_max_tokens)
                    if resolved_tokens < user_tokens:
                        log.warning("Max new tokens clamped to %d (admin ceiling)", admin_max_tokens)
                        if __event_emitter__:
                            await __event_emitter__(
                                {
                                    "type": "notification",
                                    "data": {
                                        "type": "warning",
                                        "content": f"\u26a0\ufe0f Max new tokens clamped to {admin_max_tokens} (admin ceiling).",
                                    },
                                }
                            )
                else:
                    resolved_tokens = _DEFAULT_MAX_NEW_TOKENS

            # =================================================================
            # Resolve num_beams with ceiling policy
            # =================================================================
            def _get_user_beams():
                if user_valves and user_valves.num_beams and user_valves.num_beams != "0":
                    return int(user_valves.num_beams)
                return 0

            def _get_admin_beam_ceiling():
                if self.valves.max_num_beams and self.valves.max_num_beams != "0":
                    return int(self.valves.max_num_beams)
                return 0

            user_beams = _get_user_beams()
            admin_ceiling = _get_admin_beam_ceiling()

            if user_beams > 0 and admin_ceiling > 0:
                resolved_beams = min(user_beams, admin_ceiling)
                if resolved_beams < user_beams:
                    log.warning("Num beams clamped to %d (admin ceiling)", admin_ceiling)
                    if __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "notification",
                                "data": {
                                    "type": "warning",
                                    "content": f"\u26a0\ufe0f Num beams clamped to {admin_ceiling} (admin ceiling).",
                                },
                            }
                        )
            elif user_beams > 0:
                resolved_beams = user_beams
            else:
                resolved_beams = admin_ceiling if admin_ceiling > 0 else _DEFAULT_NUM_BEAMS

            # =================================================================
            # Build the workflow: load from cache and parse
            # =================================================================
            raw_workflow = _load_workflow(__id__, "generate_caption.json")
            workflow = json.loads(raw_workflow)

            # =================================================================
            # Resolve nodes
            # =================================================================
            _, load_image = _resolve_node(workflow, "Load Image (URL/Path)")
            _, model_loader = _resolve_node(workflow, "Florence2ModelLoader")
            _, run_node = _resolve_node(workflow, "Florence2Run")
            output_node_id, _ = _resolve_node(workflow, "Show Text \U0001f40d")
            lora_node_id, _ = _resolve_node(workflow, "Florence2 Lora Loader")

            # =================================================================
            # Configure image source — auto-detect URL vs filename
            # =================================================================
            node_img = load_image["inputs"]
            parsed = urlparse(image)
            if parsed.scheme and parsed.netloc:
                node_img["source"] = "url"
                node_img["url"] = image
                node_img.pop("image", None)
                node_img.pop("Choose file to upload", None)
            else:
                node_img["source"] = "temp"
                node_img["image"] = image
                node_img["url"] = ""

            # =================================================================
            # Inject model and task
            # =================================================================
            model_loader["inputs"]["model"] = resolved_model
            # PixelProse LoRA only works with Florence-2-base-ft — disable the node for other models
            if resolved_model != "Florence-2-base-ft":
                workflow[lora_node_id]["disabled"] = True
            # Map detail_level to Florence-2 task, overrides resolved_task
            _DETAIL_MAP = {
                "simple": "caption",
                "detailed": "detailed_caption",
                "verbose": "more_detailed_caption",
            }
            detail_task = _DETAIL_MAP.get(detail_level)
            injected_task = detail_task if detail_task else resolved_task
            run_node["inputs"]["task"] = injected_task
            run_node["inputs"]["max_new_tokens"] = resolved_tokens
            run_node["inputs"]["num_beams"] = resolved_beams
            run_node["inputs"]["do_sample"] = user_valves.do_sample if user_valves else False
            # Seed: UserValve. -1 = random, >=0 = fixed
            user_seed = int(user_valves.seed) if user_valves and user_valves.seed != -1 else -1
            seed_arg = _random.randint(0, _COMFY_SEED_MAX) if user_seed == -1 else min(user_seed, _COMFY_SEED_MAX)
            run_node["inputs"]["seed"] = seed_arg

            log.info(
                "Dispatching caption workflow to ComfyUI (%s) - model=%s, task=%s, "
                "max_new_tokens=%d, num_beams=%d, detail_level=%s",
                image_config.COMFYUI_BASE_URL,
                resolved_model,
                injected_task,
                resolved_tokens,
                resolved_beams,
                detail_level,
            )

            # =================================================================
            # Execute workflow via ComfyUI API
            # =================================================================
            comfy_base = image_config.COMFYUI_BASE_URL.rstrip("/")
            api_key = image_config.COMFYUI_API_KEY or ""

            async with httpx.AsyncClient() as client:
                prompt_id = await _comfyui_queue_prompt(
                    client, comfy_base, api_key, workflow
                )

                log.info("Caption workflow queued - prompt_id=%s", prompt_id)

                try:
                    history_entry = await _comfyui_wait_for_output(
                        client, comfy_base, api_key, prompt_id
                    )
                except asyncio.CancelledError:
                    log.info("Caption cancelled by user - interrupting ComfyUI")
                    await _comfyui_interrupt(comfy_base, api_key)
                    raise

            # =================================================================
            # Extract caption text
            # =================================================================
            outputs = history_entry["outputs"]
            caption = _extract_caption(outputs, output_node_id)

            log.info(
                "Caption generated - prompt_id=%s, length=%d chars",
                prompt_id,
                len(caption),
            )

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2705 Caption generated.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            return caption

        except asyncio.CancelledError:
            log.info("generate_caption cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Caption generation cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return (
                "The caption generation was cancelled by the user. "
                "Do not retry. Acknowledge the cancellation and wait for their next request."
            )
        except Exception as e:
            log.exception("generate_caption failed: %s", e)
            return f"Error generating caption: {e}"

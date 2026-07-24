"""
title: Smart Generate Image
author: A. Martin
description: Generate images through ComfyUI with seed, model, size, and steps control
version: 3.0
"""

import asyncio
import json
import logging
import math
import random as _random
import uuid

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Shared dropdown options for the steps valve (15 down to 1, plus "System default")
_STEPS_OPTIONS = [
    {"value": str(i), "label": str(i)}
    for i in range(15, 0, -1)
]
_STEPS_OPTIONS.insert(0, {"value": "0", "label": "System default"})



# =============================================================================
# Inline workflow JSON — zit.json
# =============================================================================
_ZIT_WORKFLOW_JSON_RAW = r"""{
  "41": {
    "inputs": {
      "width": [
        "69",
        0
      ],
      "height": [
        "69",
        1
      ],
      "batch_size": 1
    },
    "class_type": "EmptyLatentImage",
    "_meta": {
      "title": "Empty Latent Image"
    }
  },
  "42": {
    "inputs": {
      "stop_at_clip_layer": -2,
      "clip": [
        "68",
        0
      ]
    },
    "class_type": "CLIPSetLastLayer",
    "_meta": {
      "title": "CLIP Set Last Layer"
    }
  },
  "43": {
    "inputs": {
      "conditioning": [
        "63",
        0
      ]
    },
    "class_type": "ConditioningZeroOut",
    "_meta": {
      "title": "ZeroOut"
    }
  },
  "51": {
    "inputs": {
      "vae_name": "Z-Image_natural_vae.safetensors"
    },
    "class_type": "VAELoader",
    "_meta": {
      "title": "Load VAE"
    }
  },
  "52": {
    "inputs": {
      "samples": [
        "66",
        0
      ],
      "vae": [
        "51",
        0
      ]
    },
    "class_type": "VAEDecode",
    "_meta": {
      "title": "VAE Decode"
    }
  },
  "58": {
    "inputs": {
      "unet_name": "zImageTurbo-mxfp8.safetensors",
      "weight_dtype": "default"
    },
    "class_type": "UNETLoader",
    "_meta": {
      "title": "Load Diffusion Model"
    }
  },
  "61": {
    "inputs": {
      "images": [
        "52",
        0
      ]
    },
    "class_type": "PreviewImage",
    "_meta": {
      "title": "Preview Image"
    }
  },
  "63": {
    "inputs": {
      "text": "{{PROMPT}}",
      "clip": [
        "42",
        0
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "CLIP Text Encode (Prompt)"
    }
  },
  "66": {
    "inputs": {
      "seed": {{SEED}},
      "steps": 10,
      "cfg": 1,
      "sampler_name": "euler",
      "scheduler": "simple",
      "denoise": 1,
      "model": [
        "422",
        0
      ],
      "positive": [
        "63",
        0
      ],
      "negative": [
        "43",
        0
      ],
      "latent_image": [
        "41",
        0
      ]
    },
    "class_type": "KSampler",
    "_meta": {
      "title": "KSampler"
    }
  },
  "68": {
    "inputs": {
      "clip_name": "qwen3_4b_instruct_2507_mxfp8.safetensors",
      "type": "lumina2",
      "device": "default"
    },
    "class_type": "CLIPLoader",
    "_meta": {
      "title": "Load CLIP"
    }
  },
  "69": {
    "inputs": {
      "megapixel": "1.0",
      "aspect_ratio": "2:3 (Classic Portrait)",
      "divisible_by": "64",
      "custom_ratio": true,
      "custom_aspect_ratio": [
        "84",
        0
      ]
    },
    "class_type": "FluxResolutionNode",
    "_meta": {
      "title": "Flux Resolution Calc"
    }
  },
  "84": {
    "inputs": {
      "string_a": "",
      "string_b": "",
      "delimiter": ":"
    },
    "class_type": "StringConcatenate",
    "_meta": {
      "title": "Aspect ratio"
    }
  },
  "422": {
    "inputs": {
      "PowerLoraLoaderHeaderWidget": {
        "type": "PowerLoraLoaderHeaderWidget"
      },
      "lora_1": {
        "on": false,
        "lora": "",
        "strength": 1
      },
      "\u2795 Add Lora": "",
      "model": [
        "58",
        0
      ]
    },
    "class_type": "Power Lora Loader (rgthree)",
    "_meta": {
      "title": "Power Lora Loader (rgthree)"
    }
  }
}
"""

# =============================================================================
# Node ID constants
# =============================================================================
NODE_EMPTY_LATENT = "41"
NODE_CLIP_SET_LAYER = "42"
NODE_ZERO_OUT = "43"
NODE_VAE_LOADER = "51"
NODE_VAE_DECODE = "52"
NODE_UNET_LOADER = "58"
NODE_PREVIEW_IMAGE = "61"
NODE_CLIP_TEXT = "63"
NODE_KSAMPLER = "66"
NODE_CLIP_LOADER = "68"
NODE_FLUX_RESOLUTION = "69"
NODE_ASPECT_RATIO = "84"
NODE_LORA = "422"

# =============================================================================
# ComfyUI constants
# =============================================================================
_COMFY_SEED_MAX: int = 1125899906842624
_COMFY_QUEUE_MAX_RETRIES = 600       # ~10 min at 1s intervals
_COMFY_QUEUE_POLL_INTERVAL = 1.0     # seconds


# =============================================================================
# Placeholder injection
# =============================================================================

def _inject_placeholders(raw_json: str, replacements: dict[str, object]) -> str:
    """
    Replace {{PLACEHOLDER}} patterns in the raw JSON string with actual values.

    This happens *before* JSON parsing, so placeholders can appear in both
    string values ("{{PROMPT}}") and numeric contexts ("seed": {{SEED}}).
    """
    for key, value in replacements.items():
        placeholder = "{{" + key + "}}"
        raw_json = raw_json.replace(placeholder, str(value))
    return raw_json


# =============================================================================
# ComfyUI API helpers (direct REST calls, no Open WebUI dependency)
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
    """Poll /history/{prompt_id} until the workflow completes. Returns the output dict."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    history_url = f"{base_url.rstrip('/')}/history/{prompt_id}"

    for attempt in range(_COMFY_QUEUE_MAX_RETRIES):
        resp = await client.get(history_url, headers=headers, timeout=10)
        resp.raise_for_status()
        history = resp.json()

        if prompt_id in history and history[prompt_id].get("outputs"):
            return history[prompt_id]["outputs"]

        if prompt_id in history and history[prompt_id].get("status", {}).get("completed") is False:
            await asyncio.sleep(_COMFY_QUEUE_POLL_INTERVAL)
            continue

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


def _extract_image_filename(outputs: dict, output_node_id: str) -> tuple[str, str]:
    """
    Extract the image filename and type from the workflow outputs.

    Returns (filename, type). type is "output" or "temp" depending on
    whether the node saved to disk or only kept the result in memory.
    """
    node_output = outputs.get(output_node_id, {})

    for key in ("images",):
        items = node_output.get(key, [])
        if items and isinstance(items, list) and len(items) > 0:
            filename = items[0].get("filename")
            img_type = items[0].get("type", "output")
            if filename:
                return (filename, img_type)

    raise RuntimeError(
        f"Could not find an image filename in output node {output_node_id}. "
        f"Available outputs: {json.dumps(node_output, indent=2)}"
    )






# =============================================================================
# TOOLS CLASS




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

    The response includes:
      - image_md: markdown to display the image in the conversation
      - image_filename: the filename on ComfyUI (not directly accessible
        from the filesystem)

    Use image_md to show the image to the user.

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
            description="Model/checkpoint name. Overrides the workflow default. Leave empty to use the value set in the workflow.",
        )
        max_steps: str = Field(
            default="0",
            description="Maximum inference steps ceiling. 0 = force workflow default (user steps ignored). >0 = clamp user steps to this value.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _STEPS_OPTIONS,
                }
            },
        )
        default_size: str = Field(
            default="768x1152",
            description="Default image size when the LLM does not specify one. Represents a 2:3 aspect ratio (both multiples of 64, ~0.88 MP).",
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        model_name: str = Field(
            default="",
            description="Your preferred model/checkpoint. Overrides the admin valve or the workflow default.",
        )
        steps: str = Field(
            default="0",
            description="Inference steps. 0 = use workflow default.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _STEPS_OPTIONS,
                }
            },
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Override the admin valve or COMFYUI_BASE_URL for image links.",
        )
        seed: int = Field(
            default=-1,
            description="Seed. -1 = random, >=0 = fixed seed for reproducibility.",
        )
        lora_name: str = Field(
            default="",
            description="LoRA filename (e.g. 'Chroma\\\\Realistic_Chroma_Slider_alpha.safetensors'). Leave empty to skip LoRA injection.",
        )
        lora_strength: float = Field(
            default=0.0,
            description="LoRA activation strength (0.0 = disabled, 0.5-1.0 typical range).",
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

        Returns image_md (for displaying) and image_filename (for reference).
        The filename is not directly accessible from the filesystem.

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
            from open_webui.routers.images import get_image_config

            image_config = await get_image_config()

            # =================================================================
            # Resolve valves: UserValves > AdminValves > workflow default
            # =================================================================
            user_valves = (__user__ or {}).get('valves', None)

            # Model: UserValves > AdminValves > None (workflow default)
            user_model = (
                user_valves.model_name if user_valves and user_valves.model_name else ""
            )
            resolved_model = user_model or self.valves.model_name or None

            # Steps:
            #   max_steps=0 → force workflow default (ignore user steps)
            #   max_steps>0 → use UserValve + clamp
            max_steps = int(self.valves.max_steps) if self.valves.max_steps and self.valves.max_steps != "0" else 0
            resolved_steps = None

            if max_steps == 0:
                # Force workflow default – ignore user steps
                resolved_steps = None
            else:
                user_valve_steps = int(user_valves.steps) if user_valves and user_valves.steps and user_valves.steps != "0" else 0

                if user_valve_steps > 0:
                    resolved_steps = min(user_valve_steps, max_steps)
                    if resolved_steps < user_valve_steps and __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "notification",
                                "data": {
                                    "type": "warning",
                                    "content": f"\u26a0\ufe0f Steps clamped to {max_steps} (system limit).",
                                },
                            }
                        )
                # else: resolved_steps stays None → workflow default

            # Seed: UserValve. -1 = random, >=0 = fixed.
            user_seed = int(user_valves.seed) if user_valves and user_valves.seed != -1 else -1
            seed_arg = _random.randint(0, _COMFY_SEED_MAX) if user_seed == -1 else min(user_seed, _COMFY_SEED_MAX)

            # Size: from LLM param or admin valve default_size, then GCD reduction
            default_size = self.valves.default_size or "1024x1024"
            final_size = size if size and "x" in size else default_size
            width, height = tuple(map(int, final_size.split("x")))
            gcd = math.gcd(width, height)
            reduced_w = width // gcd
            reduced_h = height // gcd

            # LoRA: only inject if name is non-empty AND strength > 0
            resolved_lora_name = (
                user_valves.lora_name if user_valves and user_valves.lora_name else ""
            )
            resolved_lora_strength = (
                float(user_valves.lora_strength) if user_valves and user_valves.lora_strength else 0.0
            )
            inject_lora = bool(resolved_lora_name and resolved_lora_strength > 0)

            # Base URL: UserValves > AdminValves > COMFYUI_BASE_URL
            user_image_base_url = (
                user_valves.comfyui_image_base_url if user_valves and user_valves.comfyui_image_base_url else ""
            )
            resolved_image_base_url = (
                user_image_base_url
                or self.valves.comfyui_image_base_url
                or image_config.COMFYUI_BASE_URL
            )

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

            # =================================================================
            # Build the workflow: inject placeholders into the raw JSON
            # =================================================================
            replacements = {
                "PROMPT": prompt,
                "SEED": seed_arg,
            }

            injected_raw = _inject_placeholders(_ZIT_WORKFLOW_JSON_RAW, replacements)
            workflow = json.loads(injected_raw)

            # =================================================================
            # Apply optional overrides post-parse (only when valve is non-empty)
            # =================================================================
            if resolved_model:
                workflow[NODE_UNET_LOADER]["inputs"]["unet_name"] = resolved_model

            if resolved_steps is not None:
                workflow[NODE_KSAMPLER]["inputs"]["steps"] = resolved_steps

            # Inject aspect ratio (GCD-reduced) into the StringConcatenate node
            workflow[NODE_ASPECT_RATIO]["inputs"]["string_a"] = str(reduced_w)
            workflow[NODE_ASPECT_RATIO]["inputs"]["string_b"] = str(reduced_h)

            # LoRA injection — Power Lora Loader (rgthree)
            if inject_lora:
                workflow[NODE_LORA]["inputs"]["lora_1"]["on"] = True
                workflow[NODE_LORA]["inputs"]["lora_1"]["lora"] = resolved_lora_name
                workflow[NODE_LORA]["inputs"]["lora_1"]["strength"] = resolved_lora_strength

            log.info(
                "Dispatching image workflow to ComfyUI (%s) - prompt_len=%d, size=%s, "
                "seed=%d, steps=%s, model=%s, lora=%s",
                image_config.COMFYUI_BASE_URL,
                len(prompt),
                final_size,
                seed_arg,
                steps_label,
                resolved_model or "(workflow default)",
                f"{resolved_lora_name}@{resolved_lora_strength}" if inject_lora else "(none)",
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

                log.info("Image workflow queued - prompt_id=%s", prompt_id)

                try:
                    outputs = await _comfyui_wait_for_output(
                        client, comfy_base, api_key, prompt_id
                    )
                except asyncio.CancelledError:
                    log.info("Image generation cancelled by user - interrupting ComfyUI")
                    await _comfyui_interrupt(comfy_base, api_key)
                    raise

            # =================================================================
            # Extract image filename and build URL
            # =================================================================
            image_filename, image_type = _extract_image_filename(outputs, NODE_PREVIEW_IMAGE)

            base = resolved_image_base_url.rstrip("/")
            image_url = f"{base}/api/view?filename={image_filename}&type={image_type}"

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

            return (
                f"image_md: ![Generated image]({image_url})\n"
                f"image_filename: {image_filename}\n\n"
                "Use image_md to display the image in your response."
            )

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

"""
title: Generate Video
author: A. Martin
description: Generate videos through ComfyUI (e.g. WAN2.1 text-to-video or image-to-video)
version: 1.0
"""

import asyncio
import json
import logging
import random as _random
import uuid
from urllib.parse import urlparse, parse_qs

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# =============================================================================
# Workflow JSON - WAN2.1 Image-to-Video
# =============================================================================
# Placeholders (always injected):
#   {{PROMPT}}   — the video prompt
#   {{SEED}}     — seed value
#   {{IMAGE}}    — input image filename (always required, I2V only)
#
# Optional overrides (post-parse, only when valve is non-empty):
#   model, lora, length, negative_prompt
#
#
_VIDEO_WORKFLOW_JSON_RAW = r"""{
  "6": {
    "inputs": {
      "text": "{{PROMPT}}",
      "clip": [
        "1044",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "Positive Prompt"
    }
  },
  "7": {
    "inputs": {
      "text": "deformed face, tattoo, piercing, teeth, open mouth, deformed eyes, morphed eyes, changed identity, extra limbs, rapid movement, 3d render, low quality, morphed features, warped, camera shake, rapid zoom",
      "clip": [
        "1044",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "Negative Prompt"
    }
  },
  "8": {
    "inputs": {
      "samples": [
        "876",
        0
      ],
      "vae": [
        "39",
        0
      ]
    },
    "class_type": "VAEDecode",
    "_meta": {
      "title": "VAE Decode"
    }
  },
  "39": {
    "inputs": {
      "vae_name": "wan_2.1_vae.safetensors"
    },
    "class_type": "VAELoader",
    "_meta": {
      "title": "Load VAE"
    }
  },
  "54": {
    "inputs": {
      "shift": 3,
      "model": [
        "501",
        0
      ]
    },
    "class_type": "ModelSamplingSD3",
    "_meta": {
      "title": "ModelSamplingSD3"
    }
  },
  "97": {
    "inputs": {
      "clip_name": "umt5-xxl-encoder-Q5_K_M.gguf",
      "type": "wan"
    },
    "class_type": "CLIPLoaderGGUF",
    "_meta": {
      "title": "CLIPLoader (GGUF)"
    }
  },
  "104": {
    "inputs": {
      "strength": 0.5,
      "use_gpu": true,
      "images": [
        "8",
        0
      ]
    },
    "class_type": "FastUnsharpSharpen",
    "_meta": {
      "title": "Unsharpen mask"
    }
  },
  "405": {
    "inputs": {
      "generation_width": 480,
      "generation_height": 640,
      "aspect_ratio_preservation": "keep_input",
      "image": [
        "1043",
        0
      ]
    },
    "class_type": "WanVideoImageResizeToClosest",
    "_meta": {
      "title": "WanVideo Image Resize To Closest"
    }
  },
  "460": {
    "inputs": {
      "sage_attention": "disabled",
      "allow_compile": false,
      "model": [
        "1044",
        0
      ]
    },
    "class_type": "PathchSageAttentionKJ",
    "_meta": {
      "title": "SageAttention"
    }
  },
  "501": {
    "inputs": {
      "nag_scale": 11,
      "nag_alpha": 0.25,
      "nag_tau": 2.5,
      "input_type": "default",
      "model": [
        "460",
        0
      ],
      "conditioning": [
        "1006",
        0
      ]
    },
    "class_type": "WanVideoNAG",
    "_meta": {
      "title": "NAG HIGH"
    }
  },
  "822": {
    "inputs": {
      "unet_name": "Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v-nvfp4.safetensors",
      "weight_dtype": "default"
    },
    "class_type": "UNETLoader",
    "_meta": {
      "title": "Load Diffusion Model"
    }
  },
  "876": {
    "inputs": {
      "add_noise": "enable",
      "noise_seed": [
        "1024",
        0
      ],
      "steps": 4,
      "cfg": 1,
      "sampler_name": "euler",
      "scheduler": "simple",
      "start_at_step": 0,
      "end_at_step": 10000,
      "return_with_leftover_noise": "disable",
      "model": [
        "54",
        0
      ],
      "positive": [
        "998",
        0
      ],
      "negative": [
        "998",
        1
      ],
      "latent_image": [
        "998",
        2
      ]
    },
    "class_type": "KSamplerAdvanced",
    "_meta": {
      "title": "KSampler"
    }
  },
  "993": {
    "inputs": {
      "crop": "none",
      "clip_vision": [
        "994",
        0
      ],
      "image": [
        "1043",
        0
      ]
    },
    "class_type": "CLIPVisionEncode",
    "_meta": {
      "title": "CLIP Vision Encode"
    }
  },
  "994": {
    "inputs": {
      "clip_name": "clip_vision_h_fp8_e4m3fn.safetensors"
    },
    "class_type": "CLIPVisionLoader",
    "_meta": {
      "title": "Load CLIP Vision"
    }
  },
  "998": {
    "inputs": {
      "width": [
        "405",
        1
      ],
      "height": [
        "405",
        2
      ],
      "length": 81,
      "batch_size": 1,
      "positive": [
        "6",
        0
      ],
      "negative": [
        "1006",
        0
      ],
      "vae": [
        "39",
        0
      ],
      "clip_vision_output": [
        "993",
        0
      ],
      "start_image": [
        "405",
        0
      ]
    },
    "class_type": "WanImageToVideo",
    "_meta": {
      "title": "WanImageToVideo"
    }
  },
  "1006": {
    "inputs": {
      "conditioning_to": [
        "1007",
        0
      ],
      "conditioning_from": [
        "7",
        0
      ]
    },
    "class_type": "ConditioningConcat",
    "_meta": {
      "title": "Conditioning (Concat)"
    }
  },
  "1007": {
    "inputs": {
      "text": "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走",
      "clip": [
        "1044",
        1
      ]
    },
    "class_type": "CLIPTextEncode",
    "_meta": {
      "title": "Default Wan negative prompt"
    }
  },
  "1013": {
    "inputs": {
      "frame_rate": 36,
      "loop_count": 0,
      "filename_prefix": "wan21_output",
      "format": "video/h264-mp4",
      "pix_fmt": "yuv420p",
      "crf": 10,
      "save_metadata": true,
      "trim_to_audio": false,
      "pingpong": false,
      "save_output": true,
      "images": [
        "1052",
        0
      ]
    },
    "class_type": "VHS_VideoCombine",
    "_meta": {
      "title": "Output MP4"
    }
  },
  "1014": {
    "inputs": {
      "blend_factor": 0.75,
      "blend_mode": "normal",
      "image1": [
        "104",
        0
      ],
      "image2": [
        "8",
        0
      ]
    },
    "class_type": "ImageBlend",
    "_meta": {
      "title": "Image Blend"
    }
  },
  "1024": {
    "inputs": {
      "seed": {{SEED}}
    },
    "class_type": "easy seed",
    "_meta": {
      "title": "EasySeed"
    }
  },
  "1043": {
    "inputs": {
      "source": "temp",
      "url": "",
      "image": "{{IMAGE}}",
      "Choose file to upload": null
    },
    "class_type": "LoadImageByUrlOrPath",
    "_meta": {
      "title": "Load Image (URL/Path)"
    }
  },
  "1044": {
    "inputs": {
      "PowerLoraLoaderHeaderWidget": {
        "type": "PowerLoraLoaderHeaderWidget"
      },
      "lora_1": {
        "on": true,
        "lora": "WAN2.1/Wan2.1-I2V-14B-480P-StepDistill-CfgDistill-Lightx2v-r64-lora.safetensors",
        "strength": 1
      },
      "➕ Add Lora": "",
      "model": [
        "822",
        0
      ],
      "clip": [
        "97",
        0
      ]
    },
    "class_type": "Power Lora Loader (rgthree)",
    "_meta": {
      "title": "Power Lora Loader (rgthree)"
    }
  },
  "1051": {
    "inputs": {
      "resize_type": "scale by multiplier",
      "resize_type.scale": 2,
      "quality": "ULTRA",
      "images": [
        "1014",
        0
      ]
    },
    "class_type": "RTXVideoSuperResolution",
    "_meta": {
      "title": "RTX Video Super Resolution"
    }
  },
  "1052": {
    "inputs": {
      "multiplier": 3,
      "interp_model": [
        "1053",
        0
      ],
      "images": [
        "1051",
        0
      ]
    },
    "class_type": "FrameInterpolate",
    "_meta": {
      "title": "Frame Interpolate"
    }
  },
  "1053": {
    "inputs": {
      "model_name": "rife_v4.26.safetensors"
    },
    "class_type": "FrameInterpolationModelLoader",
    "_meta": {
      "title": "Load Frame Interpolation Model"
    }
  }
}
"""

# =============================================================================
# Node ID - output node that produces the video file
# =============================================================================
NODE_OUTPUT: str = "1013"


# =============================================================================
# ComfyUI constants
# =============================================================================
_COMFY_SEED_MAX: int = 1125899906842624

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
# ComfyUI API helpers
# =============================================================================

_COMFY_QUEUE_MAX_RETRIES = 600       # ~10 min at 1s intervals
_COMFY_QUEUE_POLL_INTERVAL = 1.0     # seconds


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

        # Check if still in queue — the key might exist but have no outputs yet
        if prompt_id in history and history[prompt_id].get("status", {}).get("completed") is False:
            await asyncio.sleep(_COMFY_QUEUE_POLL_INTERVAL)
            continue

        # Key not in history yet → still queued/processing
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


def _extract_video_filename(outputs: dict, output_node_id: str) -> str:
    """
    Extract the video filename from the workflow outputs.

    Tries common output keys used by video nodes:
      - "gifs" (VHS_VideoCombine)
      - "videos" (some custom nodes)
      - Falls back to "images"
    """
    node_output = outputs.get(output_node_id, {})

    for key in ("gifs", "videos", "images"):
        items = node_output.get(key, [])
        if items and isinstance(items, list) and len(items) > 0:
            filename = items[0].get("filename")
            if filename:
                return filename

    raise RuntimeError(
        f"Could not find a video filename in output node {output_node_id}. "
        f"Available outputs: {json.dumps(node_output, indent=2)}"
    )


# =============================================================================
# TOOL
# =============================================================================

class Tools:
    """
    Generate Video - animate a previously generated image into a video.

    Only use when the user explicitly asks to animate an image that was
    just generated.

    prompt: Video description. Translate the user's request into English
        internally, then enrich with visual motion details without changing
        the subject or scene.
    image_filename: The image_filename from the last image generation
        response. Required.
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        model_name: str = Field(
            default="",
            description="Model/checkpoint name for video generation. Overrides the workflow default. Leave empty to use what's in the workflow JSON.",
        )
        lora: str = Field(
            default="",
            description="LoRA name for video style. Leave empty for no LoRA.",
        )
        length: int = Field(
            default=0,
            description="Number of frames / video length. 0 = use workflow default.",
        )
        negative_prompt: str = Field(
            default="",
            description="Negative prompt. Leave empty to use the workflow default.",
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for video links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        model_name: str = Field(
            default="",
            description="Your preferred model/checkpoint for video. Overrides the admin valve and the workflow default.",
        )
        lora: str = Field(
            default="",
            description="Your preferred LoRA for video style. Overrides the admin valve.",
        )
        length: int = Field(
            default=0,
            description="Number of frames / video length. 0 = use admin valve or workflow default.",
        )
        negative_prompt: str = Field(
            default="",
            description="Your preferred negative prompt. Leave empty to use the admin valve or workflow default.",
        )
        seed: int = Field(
            default=-1,
            description="Seed. -1 = random, >=0 = fixed seed for reproducibility.",
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Override the admin valve or COMFYUI_BASE_URL for video links.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def generate_video(
        self,
        prompt: str,
        image_filename: str,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
    ):
        """
        Animate a previously generated image into a video.

        Only use when the user explicitly asks to animate an image.

        prompt: Video description in English, enriched with motion details.
        image_filename: The image_filename from the last image generation.
        """
        if __request__ is None:
            log.error("generate_video called without request context")
            return "Error: The tool could not be initialized."

        try:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\U0001f3ac Generating video...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            from open_webui.routers.images import get_image_config

            image_config = await get_image_config()

            # =================================================================
            # Resolve valves: UserValves > AdminValves > workflow default
            # =================================================================
            user_valves = (__user__ or {}).get("valves", None)

            # Model: UserValves > AdminValves > leave workflow default
            resolved_model = (
                user_valves.model_name if user_valves and user_valves.model_name
                else self.valves.model_name or ""
            )

            # LoRA: UserValves > AdminValves > leave workflow default
            resolved_lora = (
                user_valves.lora if user_valves and user_valves.lora
                else self.valves.lora or ""
            )

            # Length: UserValves > AdminValves > leave workflow default
            user_length = user_valves.length if user_valves and user_valves.length else 0
            resolved_length = user_length or self.valves.length or 0

            # Negative prompt: UserValves > AdminValves > leave workflow default
            resolved_neg = (
                user_valves.negative_prompt if user_valves and user_valves.negative_prompt
                else self.valves.negative_prompt or ""
            )

            # Seed: UserValve. -1 = random, >=0 = fixed
            user_seed = int(user_valves.seed) if user_valves and user_valves.seed != -1 else -1
            seed_arg = _random.randint(0, _COMFY_SEED_MAX) if user_seed == -1 else min(user_seed, _COMFY_SEED_MAX)

            # Base URL: UserValves > AdminValves > COMFYUI_BASE_URL
            user_video_base_url = (
                user_valves.comfyui_image_base_url
                if user_valves and user_valves.comfyui_image_base_url
                else ""
            )
            resolved_video_base_url = (
                user_video_base_url
                or self.valves.comfyui_image_base_url
                or image_config.COMFYUI_BASE_URL
            )

            # =================================================================
            # Build the workflow: inject placeholders into the raw JSON
            # =================================================================
            # IMAGE: the input image filename passed by the agent
            image_val = image_filename

            replacements = {
                "PROMPT": prompt,
                "SEED": seed_arg,
                "IMAGE": image_val,
            }

            injected_raw = _inject_placeholders(_VIDEO_WORKFLOW_JSON_RAW, replacements)
            workflow = json.loads(injected_raw)

            # =================================================================
            # Apply optional overrides post-parse (only when valve is non-empty)
            # =================================================================
            if resolved_model:
                workflow["822"]["inputs"]["unet_name"] = resolved_model
            if resolved_lora:
                workflow["1044"]["inputs"]["lora_1"]["on"] = True
                workflow["1044"]["inputs"]["lora_1"]["lora"] = resolved_lora
            if resolved_length:
                workflow["998"]["inputs"]["length"] = resolved_length
            if resolved_neg:
                workflow["7"]["inputs"]["text"] = resolved_neg

            log.info(
                "Dispatching video workflow to ComfyUI (%s) - prompt_len=%d, seed=%d, "
                "model=%s, lora=%s, length=%s, image=%s",
                image_config.COMFYUI_BASE_URL,
                len(prompt),
                seed_arg,
                resolved_model or "(workflow default)",
                resolved_lora or "(none — workflow default)",
                str(resolved_length) if resolved_length else "(workflow default)",
                image_filename,
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

                log.info("Video workflow queued - prompt_id=%s", prompt_id)

                try:
                    outputs = await _comfyui_wait_for_output(
                        client, comfy_base, api_key, prompt_id
                    )
                except asyncio.CancelledError:
                    log.info("Video cancelled by user - interrupting ComfyUI")
                    await _comfyui_interrupt(comfy_base, api_key)
                    raise

            # =================================================================
            # Extract video filename and build URL
            # =================================================================
            video_filename = _extract_video_filename(outputs, NODE_OUTPUT)

            base = resolved_video_base_url.rstrip("/")
            video_url = f"{base}/api/view?filename={video_filename}"

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2705 Video generated.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            # The HTML block the agent must emit to show the video
            html_block = (
                f'<style>\n'
                f'* {{ margin:0; padding:0; box-sizing:border-box; }}\n'
                f'body {{ background:#0d0d0d; display:flex; align-items:center; justify-content:center; min-height:100vh; }}\n'
                f'</style>\n'
                f'<div style="background:#222">\n'
                f'  <video controls autoplay muted loop playsinline style="width:100%;display:block">\n'
                f'    <source src="{video_url}" type="video/mp4">\n'
                f'  </video>\n'
                f'</div>'
            )

            return (
                f"video_html: {html_block}\n"
                f"video_filename: {video_filename}\n\n"
                "Paste the video_html value inside a code block in your response "
                "(triple backticks) so the frontend renders it as a video player."
            )

        except asyncio.CancelledError:
            log.info("generate_video cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Video generation cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return (
                "The video generation was cancelled by the user. "
                "Do not retry. Acknowledge the cancellation and wait for their next request."
            )
        except Exception as e:
            log.exception("generate_video failed: %s", e)
            return f"Error generating video: {e}"

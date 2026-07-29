"""
title: Smart Generate Image
author: Insecure Erasure
description: Generate images through ComfyUI with seed, model, size, and steps control
version: 3.1
"""

import asyncio
import json
import logging
import math
import random as _random
import uuid

import httpx
from pathlib import Path
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Shared dropdown options for the steps valve (15 down to 1).
# Value "0" = use workflow default (handled by Field(default="0")).
_STEPS_OPTIONS = [
    {"value": str(i), "label": str(i)}
    for i in range(15, 0, -1)
]

# Separate options for max_steps admin valve (-1 = model default, 0 = user decides)
_MAX_STEPS_OPTIONS = [
    {"value": "0", "label": "User decides"},
    {"value": "-1", "label": "Model default"},
] + [
    {"value": str(i), "label": str(i)}
    for i in range(15, 0, -1)
]

# Model family options for the model_family valve
_DEFAULT_ASPECT_RATIO = "2:3"
_DEFAULT_MEGAPIXEL = "1.0"
_DEFAULT_MODEL_FAMILY = "zit"

_MODEL_FAMILY_OPTIONS = [
    {"value": "zit", "label": "Z-Image Turbo"},
    {"value": "krea2", "label": "Krea 2"},
    {"value": "flux.2", "label": "FLUX.2 Klein"},
]

# =============================================================================
# Model family configurations
# =============================================================================
MODEL_CONFIGS = {
    "zit": {
        "model": "zImageTurbo-mxfp8.safetensors",
        "text_encoder": "qwen3_4b_instruct_2507_mxfp8.safetensors",
        "vae": "Z-Image_half_natural_vae.safetensors",
        "vae_scale_factor": 16,
        "cfg": 1.0,
        "steps": 10,
        "sampler": "euler",
        "scheduler": "simple",
        "clip_type": "lumina2",
        "sigma_selector_index": 1,
    },
    "krea2": {
        "model": "krea2_turbo_mixed_nvfp4.safetensors",
        "text_encoder": "qwen3_vl_4b_instruct_mxfp8.safetensors",
        "vae": "qwen_image_vae.safetensors",
        "vae_scale_factor": 8,
        "cfg": 1.0,
        "steps": 8,
        "sampler": "euler",
        "scheduler": "simple",
        "clip_type": "krea2",
        "sigma_selector_index": 1,
    },
    "flux.2": {
        "model": "flux-2-klein-9b-nvfp4.safetensors",
        "text_encoder": "qwen_3_8b_nvfp4.safetensors",
        "vae": "flux2-vae-small-bf16.safetensors",
        "vae_scale_factor": 64,
        "cfg": 1.0,
        "steps": 8,
        "sampler": "euler",
        "scheduler": "",
        "clip_type": "flux2",
        "sigma_selector_index": 2,
    },
}


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
# ComfyUI constants
# =============================================================================
_COMFY_SEED_MAX: int = 1125899906842624
_COMFY_QUEUE_TIMEOUT = 60           # seconds


# =============================================================================
# Workflow loader — cache/tools/<tool_id>/filename.json with bootstrap fallback
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
            "to resolve the workflow from cache. First invocation bootstraps "
            "the workflow; subsequent runs use the cached copy."
        )

    from open_webui.config import CACHE_DIR

    workflow_path = CACHE_DIR / 'tools' / tool_id / filename

    if not workflow_path.exists():
        raise FileNotFoundError(
            f"Workflow file not found at {workflow_path}. "
            "Run the tool at least once inside Open WebUI to bootstrap it, "
            f"or copy the workflow JSON manually to that path."
        )

    log.info("Loading workflow from %s", workflow_path)
    return workflow_path.read_text(encoding='utf-8')


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

    for _ in range(_COMFY_QUEUE_TIMEOUT):
        resp = await client.get(history_url, headers=headers, timeout=10)
        resp.raise_for_status()
        history = resp.json()

        if prompt_id in history and history[prompt_id].get("outputs"):
            return history[prompt_id]["outputs"]

        await asyncio.sleep(1.0)

    raise TimeoutError(
        f"ComfyUI did not finish within {_COMFY_QUEUE_TIMEOUT}s "
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
    Smart Generate Image - generate images with control over aspect ratio.

    Activate this tool from the tool selector in the chat input.

    prompt: Image generation prompt. Translate the user's request into English
        internally, then enrich with visual details without changing the subject
        or scene. Do not add superfluous details. Write the final prompt in English.
    aspect_ratio (optional): Only provide when the user explicitly requests a
        specific aspect ratio. Format as W:H (e.g., 16:9). Preferably omitted to
        use system defaults.
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )
        model_family: str = Field(
            default=_DEFAULT_MODEL_FAMILY,
            description=f"Default model family. Users can override this from their valves. Default: {_DEFAULT_MODEL_FAMILY}.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _MODEL_FAMILY_OPTIONS,
                }
            },
        )
        model_name: str = Field(
            default="",
            description="Model/checkpoint name. Overrides the workflow default. Leave empty to use the value set in the workflow.",
        )
        lora_config: str = Field(
            default="[]",
            description='JSON array of LoRAs. String=only name (strength 1.0), object={"name"|"model", "strength"}. Applied positionally. User overrides on name collision.',
        )
        default_aspect_ratio: str = Field(
            default=_DEFAULT_ASPECT_RATIO,
            description=f"Default aspect ratio when the LLM does not specify one. Format W:H (e.g. 16:9). Legacy WxH format also accepted. Default: {_DEFAULT_ASPECT_RATIO}.",
        )
        megapixel: str = Field(
            default=_DEFAULT_MEGAPIXEL,
            description=f"Target megapixel value for the generated image. Controls total resolution independent of aspect ratio. Default: {_DEFAULT_MEGAPIXEL}.",
        )
        max_steps: str = Field(
            default="0",
            description="Steps policy. 0 = user decides (no clamp). -1 = force model default (ignore user). 1-15 = clamp user steps to this ceiling.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _MAX_STEPS_OPTIONS,
                }
            },
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Override the admin valve or COMFYUI_BASE_URL for image links.",
        )
        model_family: str = Field(
            default="",
            description="Model family. Overrides the admin valve. Leave empty to use the admin valve default.",
            json_schema_extra={
                "input": {
                    "type": "select",
                    "options": _MODEL_FAMILY_OPTIONS,
                }
            },
        )
        model_name: str = Field(
            default="",
            description="Your preferred model/checkpoint. Overrides the admin valve or the workflow default.",
        )
        lora_config: str = Field(
            default="[]",
            description='JSON array of LoRAs. String=only name (strength 1.0), object={"name"|"model", "strength"}. Empty name or strength 0 disables it. Applied positionally to lora_1..lora_N. Ex: ["lora1.sft", {"name": "lora2.sft", "strength": 0.5}]',
        )
        override_system_loras: bool = Field(
            default=True,
            description="When enabled, user LoRAs override system (admin) LoRAs on name collision. "
                        "Disable to use only system LoRAs regardless of user config.",
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
        seed: int = Field(
            default=-1,
            description="Seed. -1 = random, >=0 = fixed seed for reproducibility.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def smart_generate_image(
        self,
        prompt: str,
        aspect_ratio: str | None = None,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __id__: str = "",
    ):
        """
        Generate one image with optional control over aspect ratio.

        prompt: Image generation prompt. Translate the user's request into English internally,
            then enrich with visual details without changing the subject or scene. Do not add
            superfluous details. Write the final prompt in English.
        aspect_ratio (optional): ALWAYS omit this parameter unless the user's request
            specifically mentions dimensions or aspect ratio (e.g., "16:9", "square",
            "portrait", "landscape"). Format W:H (e.g., 16:9).
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

            # Model family: UserValves > AdminValves > built-in default
            model_family = (
                user_valves.model_family if user_valves and user_valves.model_family
                else self.valves.model_family or _DEFAULT_MODEL_FAMILY
            )
            if model_family not in MODEL_CONFIGS:
                model_family = _DEFAULT_MODEL_FAMILY
            model_cfg = MODEL_CONFIGS[model_family]

            # Model: UserValves > AdminValves > model_cfg
            user_model = (
                user_valves.model_name if user_valves and user_valves.model_name else ""
            )
            resolved_model = user_model or self.valves.model_name or model_cfg["model"]

            # Steps:
            #   max_steps=0  → user decides (no clamp)
            #   max_steps=-1 → force model config default (ignore user)
            #   max_steps>0  → clamp user steps to this ceiling
            model_default_steps = model_cfg["steps"]
            raw_max_steps = self.valves.max_steps
            max_steps = int(raw_max_steps) if raw_max_steps and raw_max_steps != "0" else 0
            if raw_max_steps == "-1":
                max_steps = -1  # preserve -1 (0 is a valid value in the dropdown)

            def _get_user_steps():
                if user_valves and user_valves.steps and user_valves.steps != "0":
                    return int(user_valves.steps)
                return 0

            if max_steps == -1:
                # Force model config default – ignore user steps
                resolved_steps = model_default_steps
            elif max_steps == 0:
                # User decides, no clamp
                user_valve_steps = _get_user_steps()
                resolved_steps = user_valve_steps if user_valve_steps > 0 else model_default_steps
            else:
                # max_steps > 0: clamp user steps
                user_valve_steps = _get_user_steps()
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
                else:
                    resolved_steps = model_default_steps

            # Seed: UserValve. -1 = random, >=0 = fixed.
            user_seed = int(user_valves.seed) if user_valves and user_valves.seed != -1 else -1
            seed_arg = _random.randint(0, _COMFY_SEED_MAX) if user_seed == -1 else min(user_seed, _COMFY_SEED_MAX)

            # Aspect ratio: from LLM param or admin valve default, normalised by GCD
            raw_aspect = aspect_ratio or self.valves.default_aspect_ratio or _DEFAULT_ASPECT_RATIO
            if "x" in raw_aspect or "X" in raw_aspect:
                raw_aspect = raw_aspect.lower().replace("x", ":")
            parts = raw_aspect.split(":")
            if len(parts) != 2:
                raise ValueError(
                    f"Invalid aspect ratio format: {raw_aspect!r}. "
                    "Use W:H (e.g. 16:9) or WxH (e.g. 1920x1080)."
                )
            try:
                w, h = int(parts[0]), int(parts[1])
            except ValueError:
                raise ValueError(
                    f"Invalid numbers in aspect ratio: {raw_aspect!r}. "
                    "Both parts must be integers."
                )
            gcd = math.gcd(w, h)
            reduced_w = w // gcd
            reduced_h = h // gcd

            # LoRA: validate and combine admin + user. User wins on name collision.
            # Pop: user can disable a LoRA with strength=0 to free the slot.
            def _lora_name(item):
                if isinstance(item, str):
                    return item
                if isinstance(item, dict):
                    return item.get("name", item.get("model", ""))
                return ""

            def _load_loras(raw: str, label: str):
                """Parse a lora_config JSON string. Returns (list, error_or_None)."""
                if not raw or raw.strip() == "":
                    return [], None
                try:
                    p = json.loads(raw)
                except json.JSONDecodeError as e:
                    return [], f"Invalid JSON in {label} lora_config: {e}"
                except TypeError as e:
                    return [], f"Invalid type in {label} lora_config: {e}"
                if not isinstance(p, list):
                    return [], f"""{label} lora_config must be a JSON array, got {type(p).__name__}. Ex: ["lora.sft"]"""
                for i, item in enumerate(p):
                    if isinstance(item, str):
                        continue
                    if isinstance(item, dict):
                        name = item.get("name", item.get("model", None))
                        if name is not None and not isinstance(name, str):
                            return [], f"{label} lora_config[{i}] 'name'/'model' must be a string, got {type(name).__name__}"
                        strength = item.get("strength", None)
                        if strength is not None and not isinstance(strength, (int, float)):
                            return [], f"{label} lora_config[{i}] 'strength' must be a number, got {type(strength).__name__}"
                    else:
                        return [], f"{label} lora_config[{i}] must be a string or object, got {type(item).__name__}"
                return p, None

            admin_loras, err = _load_loras(self.valves.lora_config, "admin")
            if err:
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "error",
                                "content": f"LoRA config error: {err}",
                            },
                        }
                    )
                return f"Error: {err}"

            user_loras = []
            if user_valves and user_valves.override_system_loras and user_valves.lora_config:
                user_loras, err = _load_loras(user_valves.lora_config, "user")
                if err:
                    if __event_emitter__:
                        await __event_emitter__(
                            {
                                "type": "notification",
                                "data": {
                                    "type": "error",
                                    "content": f"LoRA config error: {err}",
                                },
                            }
                        )
                    return f"Error: {err}"

            user_active = []
            blocked_names = set()
            for item in user_loras:
                name = _lora_name(item)
                if not name:
                    continue
                disabled = False
                if isinstance(item, dict):
                    s = float(item.get("strength", 1.0))
                    if s == 0:
                        disabled = True
                if disabled:
                    blocked_names.add(name)
                else:
                    user_active.append(item)
                    blocked_names.add(name)

            combined = list(user_active)
            for item in admin_loras:
                if _lora_name(item) not in blocked_names:
                    combined.append(item)

            # Build LoRA lines for status
            lora_desc_lines = []
            if combined:
                for item in combined:
                    if isinstance(item, str):
                        lora_desc_lines.append(f"{item} = 1.0")
                    elif isinstance(item, dict):
                        name = item.get("name", item.get("model", "?"))
                        strength = item.get("strength", 1.0)
                        lora_desc_lines.append(f"{name} = {strength}")

            # Base URL: UserValves > AdminValves > COMFYUI_BASE_URL
            user_image_base_url = (
                user_valves.comfyui_image_base_url if user_valves and user_valves.comfyui_image_base_url else ""
            )
            resolved_image_base_url = (
                user_image_base_url
                or self.valves.comfyui_image_base_url
                or image_config.COMFYUI_BASE_URL
            )

            if __event_emitter__:
                status_desc = "\U0001f3a8 Generating image"
                if lora_desc_lines:
                    status_desc += " with LoRAs..."
                    for line in lora_desc_lines:
                        status_desc += f"\n    \u2022 {line}"
                else:
                    status_desc += "..."
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": status_desc,
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            # =================================================================
            # Build the workflow: load raw JSON and parse it directly
            # =================================================================
            raw_workflow = _load_workflow(__id__, "smart_generate_image.json")
            workflow = json.loads(raw_workflow)

            # =================================================================
            # Resolve workflow nodes by _meta.title (unique identifiers)
            # =================================================================
            _, unet_loader = _resolve_node(workflow, "Load Diffusion Model")
            _, clip_loader = _resolve_node(workflow, "Load CLIP")
            _, vae_loader = _resolve_node(workflow, "Load VAE")
            _, flux_resolution = _resolve_node(workflow, "Flux Resolution Calc")
            _, aspect_ratio = _resolve_node(workflow, "Aspect ratio")
            _, prompt_node = _resolve_node(workflow, "Prompt")
            _, text_encoder = _resolve_node(workflow, "CLIP Text Encode (Prompt)")
            _, steps_node = _resolve_node(workflow, "Steps")
            _, cfg_guider = _resolve_node(workflow, "CFGGuider")
            _, sampler_select = _resolve_node(workflow, "KSamplerSelect")
            _, basic_scheduler = _resolve_node(workflow, "BasicScheduler")
            _, sigma_switch = _resolve_node(workflow, "Switch (SIGMAS)")
            _, random_noise = _resolve_node(workflow, "RandomNoise")
            _, lora_node = _resolve_node(workflow, "Power Lora Loader (rgthree)")
            preview_image_id, _ = _resolve_node(workflow, "Preview Image")

            # =================================================================
            # Inject model config values into workflow nodes
            # =================================================================

            # Diffusion model
            unet_loader["inputs"]["unet_name"] = resolved_model

            # Text encoder (CLIP)
            clip_loader["inputs"]["clip_name"] = model_cfg["text_encoder"]
            clip_loader["inputs"]["type"] = model_cfg["clip_type"]

            # VAE
            vae_loader["inputs"]["vae_name"] = model_cfg["vae"]

            # Resolution: megapixel + aspect ratio + divisible_by
            flux_resolution["inputs"]["megapixel"] = self.valves.megapixel or _DEFAULT_MEGAPIXEL
            aspect_ratio["inputs"]["string_a"] = str(reduced_w)
            aspect_ratio["inputs"]["string_b"] = str(reduced_h)
            flux_resolution["inputs"]["aspect_ratio"] = "2:3 (Classic Portrait)"
            flux_resolution["inputs"]["divisible_by"] = str(model_cfg["vae_scale_factor"])

            # Prompt
            prompt_node["inputs"]["value"] = prompt

            # Steps
            steps_node["inputs"]["value"] = resolved_steps
            basic_scheduler["inputs"]["steps"] = resolved_steps

            # Seed -> RandomNoise
            random_noise["inputs"]["noise_seed"] = seed_arg

            # CFG
            cfg_guider["inputs"]["cfg"] = model_cfg["cfg"]

            # Sampler
            sampler_select["inputs"]["sampler_name"] = model_cfg["sampler"]

            # Scheduler (BasicScheduler) - empty for flux.2 (uses Flux2Scheduler internally)
            # Only override if non-empty; otherwise leave the workflow default ("simple")
            # to avoid ComfyUI validation errors on unused-but-connected nodes.
            if model_cfg["scheduler"]:
                basic_scheduler["inputs"]["scheduler"] = model_cfg["scheduler"]

            # Sigma selector: 1 = BasicScheduler (ZIT), 2 = Flux2Scheduler (FLUX.2)
            sigma_switch["inputs"]["select"] = model_cfg["sigma_selector_index"]

            # =================================================================
            # Inject LoRAs
            # =================================================================
            max_slots = sum(1 for k in lora_node["inputs"] if k.startswith("lora_"))
            lora_config = combined[:max_slots]

            log.info("LoRA injection: admin_raw=%s user_raw=%s combined=%s",
                      self.valves.lora_config,
                      user_valves.lora_config if user_valves else "(no user)",
                      json.dumps(lora_config))

            for i, item in enumerate(lora_config, start=1):
                slot = f"lora_{i}"
                if slot not in lora_node["inputs"]:
                    break  # no more slots in the workflow
                if isinstance(item, str):
                    name = item
                    strength = 1.0
                elif isinstance(item, dict):
                    name = item.get("name", item.get("model", ""))
                    strength = float(item.get("strength", 1.0))
                else:
                    continue  # skip invalid entries

                if bool(name) and strength != 0:
                    lora_node["inputs"][slot]["on"] = True
                    lora_node["inputs"][slot]["lora"] = name
                    lora_node["inputs"][slot]["strength"] = strength
                else:
                    # Desactivado: vacío todo para que ComfyUI no cargue el modelo
                    lora_node["inputs"][slot]["on"] = False
                    lora_node["inputs"][slot]["lora"] = ""
                    lora_node["inputs"][slot]["strength"] = 0

            log.info(
                "Dispatching image workflow to ComfyUI (%s) - family=%s, prompt_len=%d, "
                "aspect_ratio=%s, seed=%d, steps=%d, model=%s, loras=%s",
                image_config.COMFYUI_BASE_URL,
                model_family,
                len(prompt),
                f"{reduced_w}:{reduced_h}",
                seed_arg,
                resolved_steps,
                resolved_model,
                json.dumps(lora_config) if lora_config else "(none)",
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
            image_filename, image_type = _extract_image_filename(outputs, preview_image_id)

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

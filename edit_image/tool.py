"""
title: Edit Image
author: Insecure Erasure
description: Edit a previously generated image using inpainting/editing
version: 1.4
"""

import asyncio
import html
import json
import logging
import random as _random
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import re
from pydantic import BaseModel, Field

from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)

# ComfyUI seed max (consistent with smart_generate_image)
_COMFY_SEED_MAX: int = 1125899906842624
_COMFY_QUEUE_TIMEOUT = 60           # seconds

# Steps dropdown options (1-15). Value "0" = use workflow default (handled by Field(default="0")).
_STEPS_OPTIONS = [
    {"value": str(v), "label": str(v)} for v in range(1, 16)
]

# Restoration mode (mode="restore"): the runtime LoRA appended AFTER any
# admin/user LoRAs (strength 1.0) and the restoration prompt. The agent's
# prompt is optional in restore mode; when provided, it is appended
# after the restoration prefix.
_RESTORE_LORA_NAME: str = "Flux2-Klein-Image-RestoreV1.safetensors"
_RESTORE_PROMPT_PREFIX: str = (
    "restore the image quality, remove any compression artifacts, remove any "
    "haze and soft edges, enrich the original with new intricate detail in "
    "all textures and surfaces creating a professional photorealistic "
    "photograph with natural lighting and skin texture,"
)


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
            f"Copy {filename} from the tool's directory to that path."
        )

    log.info("Loading workflow from %s", workflow_path)
    return workflow_path.read_text(encoding='utf-8')

# =============================================================================
# Embed template loader — cache/tools/<tool_id>/<tool>.html
# =============================================================================

def _load_embed(tool_id: str, filename: str) -> str:
    """
    Load the embed HTML template from the tool's cache directory.

    Resolves CACHE_DIR / 'tools' / <tool_id> / <filename>. Returns the raw
    HTML string; the _build_* methods inject their values into it.

    Raises RuntimeError if the tool_id is empty or the file is not found.
    """
    if not tool_id:
        raise RuntimeError(
            "No tool_id provided. The tool must run inside Open WebUI "
            "to resolve the embed template from cache."
        )

    from open_webui.config import CACHE_DIR

    embed_path = CACHE_DIR / 'tools' / tool_id / filename

    if not embed_path.exists():
        raise FileNotFoundError(
            f"Embed template not found at {embed_path}. "
            f"Copy {filename} from the tool's directory to that path."
        )

    log.info("Loading embed template from %s", embed_path)
    return embed_path.read_text(encoding='utf-8')



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


class Tools:
    """
    Edit a previously generated image.

    Only call when the user explicitly asks to edit or modify an existing
    image that was generated in this conversation. Pass an image filename
    or a direct URL.

    The prompt describes the process to apply in natural language
    (e.g. "Make the cat wear a top hat", "Change the background to a
    beach at sunset", or "Restore this image to full quality").

    mode="edit" (default) applies the edit normally. Pass mode="restore"
    when the user wants to restore or enhance the quality of a degraded
    image (compression artifacts, haze, soft edges, low detail). In restore
    mode prompt is optional (omit it or pass an empty string): the
    restoration prompt is used on its own.
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        steps: str = Field(
            default="0",
            description="Inference steps (1-15). 0 = use workflow default (6).",
            enum=[o["value"] for o in _STEPS_OPTIONS],
        )
        lora_config: str = Field(
            default="[]",
            description='JSON array of LoRAs. String=only name (strength 1.0), object={"name"|"model", "strength"}. Applied positionally. Empty name or strength 0 disables it.',
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        steps: str = Field(
            default="0",
            description="Inference steps (1-15). 0 = use workflow default (6).",
            enum=[o["value"] for o in _STEPS_OPTIONS],
        )
        lora_config: str = Field(
            default="[]",
            description='JSON array of LoRAs. String=only name (strength 1.0), object={"name"|"model", "strength"}. Empty name or strength 0 disables it. Applied positionally to lora_1..lora_N. Ex: ["lora1.sft", {"name": "lora2.sft", "strength": 0.5}]',
        )
        override_system_loras: bool = Field(
            default=False,
            description="When enabled, user LoRAs replace system (admin) LoRAs entirely. "
                        "When disabled (default), system LoRAs take priority and user LoRAs "
                        "are only added if they don't collide with system ones.",
        )
        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links. Overrides the admin valve and COMFYUI_BASE_URL.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    def _build_compare_slider(
        self,
        image_a: str,
        image_b: str,
        gallery: bool = False,
        prompt: str | None = None,
        tool_id: str = "",
    ) -> str:
        """
        Build the before/after comparison slider embed.
        
        The markup lives in edit_image.html (loaded from the tool's cache
        directory); gallery markers/caption behavior is documented in the
        header comment of that file and in DESIGN.md §10–12.
        """
        a = html.escape(image_a, quote=True)
        b = html.escape(image_b, quote=True)
        gallery_attr = ' class="viewer" data-gallery="1"' if gallery else ''
        prompt_attr = f' data-prompt="{html.escape(prompt, quote=True)}"' if prompt else ''
        template = _load_embed(tool_id, "edit_image.html")
        return re.sub(
            r"\{(\w+)\}",
            lambda m: {"a": a, "b": b, "gallery_attr": gallery_attr, "prompt_attr": prompt_attr}[m.group(1)],
            template,
        )
    async def edit_image(
        self,
        image: str,
        prompt: str = "",
        mode: str = "edit",
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __id__: str = "",
    ):
        """
        Edit a previously generated image.

        Only call when the user explicitly asks to edit or modify an
        existing image. Pass an image filename or a direct URL.

        The edited image is displayed in the chat as a Rich UI embed (a
        before/after comparison slider, original vs edited, the same embed as
        compare_images). The tool returns the image URL as context
        ({'image': <url>}); use it for chained tool calls (upscale_image,
        virtual_try_on, generate_video) or to refer to the edited image.

        :param image: The filename previously generated from
            smart_generate_image or upscale_image, or a direct URL to an
            external image.
        :param prompt: Natural language description of the process to
            apply (e.g., "Change the cat's fur to orange", "Add a sunset
            background", or "Restore this image to full quality"). Be
            specific and descriptive. In "restore" mode it is optional
            (omit it or pass ""): the restoration prompt is used on its
            own.
        :param mode: "edit" (default) applies the edit normally.
            "restore" restores/enhances the quality of a degraded image.
            Use it when the user asks to restore, deblur, denoise,
            de-haze or improve the quality of an image.
        """
        if __request__ is None:
            log.error("edit_image called without request context")
            return "Error: The tool could not be initialized."

        # mode: "edit" (default) or "restore"
        mode = (mode or "edit").strip().lower()
        if mode not in ("edit", "restore"):
            return (
                f"Error: invalid mode {mode!r}. mode must be 'edit' (default) "
                "or 'restore'."
            )
        restore = mode == "restore"
        verb = "Restoring" if restore else "Editing"
        done_label = "Image restored." if restore else "Image edited."

        try:
            # Immediate feedback: let the user know editing has started
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"\U0001f3a8 {verb} image...",
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
            user_valves = (__user__ or {}).get('valves', None)

            # Steps: UserValve > AdminValve > workflow default (6)
            resolved_steps = None
            user_valve_steps = int(user_valves.steps) if user_valves and user_valves.steps and user_valves.steps != "0" else 0
            admin_valve_steps = int(self.valves.steps) if self.valves.steps and self.valves.steps != "0" else 0

            if user_valve_steps > 0:
                resolved_steps = user_valve_steps
            elif admin_valve_steps > 0:
                resolved_steps = admin_valve_steps

            # LoRA: validate and combine admin + user. User wins on name collision.
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

            async def _check_loras_exist(lora_list, comfy_base_url, api_key=""):
                """Check that LoRA filenames exist on the ComfyUI server. Returns list of missing names."""
                if not lora_list:
                    return []
                names_to_check = set()
                for item in lora_list:
                    if isinstance(item, str):
                        names_to_check.add(item.replace("\\", "/").rsplit("/", 1)[-1])
                    elif isinstance(item, dict):
                        name = item.get("name", item.get("model", ""))
                        if name:
                            names_to_check.add(name.replace("\\", "/").rsplit("/", 1)[-1])
                if not names_to_check:
                    return []
                try:
                    headers = {}
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(
                            f"{comfy_base_url.rstrip('/')}/models/loras",
                            headers=headers,
                            timeout=10,
                        )
                        resp.raise_for_status()
                        server_loras = resp.json()
                        server_basenames = {
                            p.replace("\\", "/").rsplit("/", 1)[-1] for p in server_loras
                        }
                        return [n for n in names_to_check if n not in server_basenames]
                except Exception:
                    return []

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
            if user_valves and user_valves.lora_config:
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
            for item in user_loras:
                name = _lora_name(item)
                if not name:
                    continue
                if isinstance(item, dict):
                    s = float(item.get("strength", 1.0))
                    if s == 0:
                        continue
                user_active.append(item)

            if user_valves and user_valves.override_system_loras:
                # User overrides system — only user LoRAs, admin ignored
                combined = list(user_active)
            else:
                # System wins on collision, user adds non-colliding LoRAs
                system_names = {_lora_name(item) for item in admin_loras}
                combined = list(admin_loras)
                for item in user_active:
                    if _lora_name(item) not in system_names:
                        combined.append(item)

            if restore:
                # Runtime restoration LoRA — appended last, strength 1.0
                combined.append({"name": _RESTORE_LORA_NAME, "strength": 1.0})

            # Validate LoRAs exist on the ComfyUI server
            missing = await _check_loras_exist(
                combined,
                image_config.COMFYUI_BASE_URL,
                image_config.COMFYUI_API_KEY or "",
            )
            if missing:
                msg = f"LoRA(s) not found on server: {', '.join(missing)}"
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "notification",
                            "data": {
                                "type": "error",
                                "content": msg,
                            },
                        }
                    )
                return f"Error: {msg}"

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

            # =================================================================
            # Resolve image base URL for the output link
            #   UserValves > AdminValves > COMFYUI_BASE_URL
            # =================================================================
            user_image_base_url = (
                user_valves.comfyui_image_base_url
                if user_valves and user_valves.comfyui_image_base_url
                else ""
            )
            resolved_image_base_url = (
                user_image_base_url
                or self.valves.comfyui_image_base_url
                or image_config.COMFYUI_BASE_URL
            )

            # Progress update: show resolved LoRAs if any
            if __event_emitter__ and lora_desc_lines:
                status_desc = f"\U0001f3a8 {verb} image with LoRAs..."
                for line in lora_desc_lines:
                    status_desc += f"\n    \u2022 {line}"
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
            # Build the workflow: load from cache and parse
            # =================================================================
            raw_workflow = _load_workflow(__id__, "edit_image.json")
            workflow = json.loads(raw_workflow)

            # =================================================================
            # 1. Configure image source — auto-detect URL vs filename
            # =================================================================
            _, load_image = _resolve_node(workflow, "Load Image (URL/Path)")
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
            # 2. Set the edit prompt (the user-facing description)
            # =================================================================
            _, edit_node = _resolve_node(workflow, "Prompt")
            if restore:
                # Restoration mode uses the restoration prompt. The agent's
                # prompt is optional here (defaults to ""); append it only
                # when actually provided — the prefix already ends with a
                # comma, so the agent's description flows naturally after a
                # space.
                stripped = prompt.strip() if prompt else ""
                edit_node["inputs"]["value"] = (
                    _RESTORE_PROMPT_PREFIX + " " + stripped
                    if stripped
                    else _RESTORE_PROMPT_PREFIX
                )
            else:
                edit_node["inputs"]["value"] = prompt

            # =================================================================
            # 3. Generate a random seed and inject it
            # =================================================================
            seed_arg = _random.randint(0, _COMFY_SEED_MAX)
            _, ksampler = _resolve_node(workflow, "KSampler")
            ksampler["inputs"]["seed"] = seed_arg

            # =================================================================
            # 4. Apply steps override
            # =================================================================
            if resolved_steps is not None:
                ksampler["inputs"]["steps"] = resolved_steps

            # =================================================================
            # 5. Inject LoRAs into the Power Lora Loader
            # =================================================================
            _, lora_node = _resolve_node(workflow, "Power Lora Loader (rgthree)")
            preview_image_id, _ = _resolve_node(workflow, "Random Preview Image")

            # The rgthree Power Lora Loader accepts any number of lora_N
            # inputs (FlexibleOptionalInputType), so grow the workflow when
            # there are more LoRAs than it defines instead of truncating.
            max_slots = sum(1 for k in lora_node["inputs"] if k.startswith("lora_"))
            for i in range(max_slots + 1, len(combined) + 1):
                lora_node["inputs"][f"lora_{i}"] = {
                    "on": False,
                    "lora": "",
                    "strength": 0,
                }
            lora_config = combined

            log.info("LoRA injection: admin_raw=%s user_raw=%s combined=%s",
                      self.valves.lora_config,
                      user_valves.lora_config if user_valves else "(no user)",
                      json.dumps(lora_config))

            for i, item in enumerate(lora_config, start=1):
                slot = f"lora_{i}"
                if slot not in lora_node["inputs"]:
                    break
                if isinstance(item, str):
                    name = item
                    strength = 1.0
                elif isinstance(item, dict):
                    name = item.get("name", item.get("model", ""))
                    strength = float(item.get("strength", 1.0))
                else:
                    continue

                if bool(name) and strength != 0:
                    lora_node["inputs"][slot]["on"] = True
                    lora_node["inputs"][slot]["lora"] = name
                    lora_node["inputs"][slot]["strength"] = strength
                else:
                    lora_node["inputs"][slot]["on"] = False
                    lora_node["inputs"][slot]["lora"] = ""
                    lora_node["inputs"][slot]["strength"] = 0

            log.info(
                "Dispatching edit workflow to ComfyUI (%s) - mode=%s, %s=%s, seed=%d, steps=%s, loras=%s",
                image_config.COMFYUI_BASE_URL,
                mode,
                "url" if parsed.scheme and parsed.netloc else "file",
                image,
                seed_arg,
                resolved_steps or "(workflow default)",
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

                log.info("Edit workflow queued - prompt_id=%s", prompt_id)

                try:
                    outputs = await _comfyui_wait_for_output(
                        client, comfy_base, api_key, prompt_id
                    )
                except asyncio.CancelledError:
                    log.info("Edit cancelled by user - interrupting ComfyUI")
                    await _comfyui_interrupt(comfy_base, api_key)
                    raise

            # =================================================================
            # Extract image filename and build URL
            # =================================================================
            edit_filename, image_type = _extract_image_filename(outputs, preview_image_id)

            base = resolved_image_base_url.rstrip("/")
            edit_url = f"{base}/api/view?filename={edit_filename}&type={image_type}"

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"\u2705 {done_label}",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            # Rich UI embed (see DESIGN.md): the LLM receives only the
            # actionable context ({'image': url}) and never sees the HTML.
            # The result is a before/after comparison slider (the same embed
            # as compare_images, DESIGN.md §10): the ORIGINAL image vs the
            # EDITED one, in the chat embed AND in the fullscreen overlay
            # (floating maximize button, bottom-right). Both images share the
            # same aspect ratio (the edit workflow keeps the input size), so
            # the slider's single-box sizing fits both with object-fit:cover.
            # The original URL is the passthrough argument when it is a URL,
            # or the temp-file URL (type=temp — the same directory the Load
            # Image node reads from) when it is a filename from a previous
            # generation.
            if parsed.scheme and parsed.netloc:
                original_url = image
            else:
                original_url = (
                    f"{base}/api/view?filename={image}&type=temp"
                )

            slider = self._build_compare_slider(
                original_url, edit_url, gallery=True, prompt=prompt, tool_id=__id__
            )
            return HTMLResponse(
                content=slider, headers={"Content-Disposition": "inline"}
            ), {"image": edit_url}

        except asyncio.CancelledError:
            log.info("edit_image cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Image edit cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return (
                "The image edit was cancelled by the user. "
                "Do not retry. Acknowledge the cancellation and wait for their next request."
            )
        except Exception as e:
            log.exception("edit_image failed: %s", e)
            return f"Error editing image: {e}"

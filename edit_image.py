"""
title: Edit Image
author: A. Martin
description: Edit a previously generated image using Flux 2 inpainting/editing
version: 1.0
"""

import asyncio
import json
import logging
import random as _random
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# ComfyUI seed max (consistent with smart_generate_image)
_COMFY_SEED_MAX: int = 1125899906842624

# Steps dropdown options (consistent with smart_generate_image)
_STEPS_OPTIONS = [
    {"value": "0", "label": "0 (System default)"},
    *[{"value": str(v), "label": str(v)} for v in range(1, 16)],
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


class Tools:
    """
    Edit a previously generated image.

    Only call when the user explicitly asks to edit or modify an existing
    image that was generated in this conversation. Pass an image filename
    or a direct URL.

    The edit_prompt describes the desired change in natural language
    (e.g. "Make the cat wear a top hat" or "Change the background to
    a beach at sunset").
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
        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links. Overrides the admin valve and COMFYUI_BASE_URL.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    async def edit_image(
        self,
        image: str,
        edit_prompt: str,
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

        image: The filename previously generated from smart_generate_image
            or enhance_image, or a direct URL to an external image.

        edit_prompt: Natural language description of the edit to apply
            (e.g., "Change the cat's fur to orange", "Add a sunset
            background"). Be specific and descriptive.
        """
        if __request__ is None:
            log.error("edit_image called without request context")
            return "Error: The tool could not be initialized."

        try:
            # Immediate feedback: let the user know editing has started
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\U0001f3a8 Editing image...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            from open_webui.routers.images import get_image_config
            from open_webui.utils.images.comfyui import (
                ComfyUICreateImageForm,
                ComfyUIWorkflow,
                comfyui_create_image,
            )
            import uuid

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
                status_desc = "\U0001f3a8 Editing image with LoRAs..."
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
            _, edit_node = _resolve_node(workflow, "Prompt input")
            edit_node["inputs"]["value"] = edit_prompt

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

            max_slots = sum(1 for k in lora_node["inputs"] if k.startswith("lora_"))
            lora_config = combined[:max_slots]

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
                "Dispatching edit workflow to ComfyUI (%s) - %s=%s, seed=%d, steps=%s, loras=%s",
                image_config.COMFYUI_BASE_URL,
                "url" if parsed.scheme and parsed.netloc else "file",
                image,
                seed_arg,
                resolved_steps or "(workflow default)",
                json.dumps(lora_config) if lora_config else "(none)",
            )

            cf_form = ComfyUICreateImageForm(
                **{
                    "prompt": "",
                    "width": "1",
                    "height": "1",
                    "n": 1,
                    "workflow": ComfyUIWorkflow(
                        **{
                            "workflow": json.dumps(workflow),
                            "nodes": [],
                        }
                    ),
                }
            )

            # =================================================================
            # Execute workflow
            # =================================================================
            try:
                res = await comfyui_create_image(
                    None,
                    cf_form,
                    str(uuid.uuid4()),
                    image_config.COMFYUI_BASE_URL,
                    image_config.COMFYUI_API_KEY,
                )
            except asyncio.CancelledError:
                log.info("Edit cancelled by user - interrupting ComfyUI")
                try:
                    interrupt_url = f"{image_config.COMFYUI_BASE_URL.rstrip('/')}/interrupt"
                    headers = {}
                    if image_config.COMFYUI_API_KEY:
                        headers["Authorization"] = f"Bearer {image_config.COMFYUI_API_KEY}"
                    async with httpx.AsyncClient() as client:
                        await client.post(interrupt_url, headers=headers, timeout=5)
                except Exception:
                    log.warning("Failed to interrupt ComfyUI", exc_info=True)
                raise

            if res is None or not res.get("data"):
                log.error("ComfyUI returned no image data")
                raise RuntimeError("ComfyUI returned no image data")

            # =================================================================
            # Build output URLs (same logic as smart_generate_image)
            # =================================================================
            images = []
            comfy_base = image_config.COMFYUI_BASE_URL.rstrip("/")
            for img in res["data"]:
                raw_url = img["url"]
                if raw_url.startswith("/"):
                    base = resolved_image_base_url.rstrip("/")
                    edit_url = f"{base}{raw_url}"
                elif raw_url.startswith(comfy_base):
                    edit_url = raw_url.replace(comfy_base, resolved_image_base_url.rstrip("/"), 1)
                else:
                    edit_url = raw_url
                images.append({"url": edit_url})

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2705 Image edited.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            edit_url = images[0]["url"] if images else None

            # Extract filename from URL (same as smart_generate_image)
            parsed = urlparse(edit_url)
            params = parse_qs(parsed.query)
            edit_filename = params.get("filename", ["unknown"])[0]

            return (
                f"image_md: ![Edited image]({edit_url})\n"
                f"image_filename: {edit_filename}\n\n"
                "Use image_md to display the edited image in your response."
            )

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

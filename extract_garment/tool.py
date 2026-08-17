"""
title: Extract Garment
author: Insecure Erasure
description: Isolate a garment from a photo (background removed, cropped to the garment) using BiRefNet + Florence-2 + SAM2 via ComfyUI. Accepts a filename from a previous generation or a direct external image URL.
version: 1.0
"""

import asyncio
import html
import json
import logging
import uuid
from urllib.parse import urlparse

import httpx
import re
from pydantic import BaseModel, Field

from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)

# =============================================================================
# ComfyUI constants
# =============================================================================
_COMFY_QUEUE_MAX_RETRIES = 600       # ~10 min at 1s intervals (BiRefNet + Florence-2 + SAM2 is slow)
_COMFY_QUEUE_POLL_INTERVAL = 1.0     # seconds

# =============================================================================
# Supported garment types
#   The model picks one of these (case/separator-insensitive). Anything else
#   is rejected with an error listing the supported values, so the model can
#   correct itself and retry.
# =============================================================================
_SUPPORTED_GARMENTS = [
    "upper garment",
    "lower garment",
    "shirt",
    "t-shirt",
    "jacket",
    "sweater",
    "pullover",
    "pants",
    "skirt",
    "trousers",
]


def _normalize_garment(value: str) -> str:
    """Normalize a garment name for comparison: lowercase, strip spaces/dashes/underscores."""
    return re.sub(r"[\s\-_]+", "", value.lower())


def _resolve_garment(value: str) -> str:
    """
    Resolve a user/model garment string to a canonical supported name.

    Returns the canonical name (e.g. 't-shirt') or '' when the value is not
    supported.
    """
    norm = _normalize_garment(value)
    for garment in _SUPPORTED_GARMENTS:
        if _normalize_garment(garment) == norm:
            return garment
    return ""


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
# TOOL
# =============================================================================

class Tools:
    """
    Extract (isolate) a garment from a photo and return it as a standalone image.
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )
        background_color: str = Field(
            default="#c1ffff",
            description="Background color used in the background-removal phase to fill the area behind the garment. Default: #c1ffff.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links. Overrides the admin valve and COMFYUI_BASE_URL.",
        )
        background_color: str = Field(
            default="",
            description="Background color for the background-removal phase. Leave empty to use the admin valve default.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False

    def _build_image_viewer(
        self,
        image_url: str,
        aspect_ratio: tuple[int, int] | None = None,
        gallery: bool = False,
        prompt: str | None = None,
        original: str | None = None,
        tool_id: str = "",
    ) -> str:
        """
        Build the image viewer embed.

        The markup lives in extract_garment.html (loaded from the tool's cache
        directory); lightbox/gallery/caption behavior is documented in the
        header comment of that file and in DESIGN.md §10–12. `original` is the
        URL of the source image the garment was extracted from, shown as a
        small bottom-left thumbnail in the frame (never in the lightbox).
        """
        src = html.escape(image_url, quote=True)
        gallery_attr = ' data-gallery="1"' if gallery else ''
        prompt_attr = f' data-prompt="{html.escape(prompt, quote=True)}"' if prompt else ''
        source_block = ""
        if original:
            o = html.escape(original, quote=True)
            source_block = (
                '<div class="source" id="source" title="Original image">'
                f'<img id="source-img" src="{o}" alt="Original image" draggable="false">'
                "</div>"
            )
        if aspect_ratio:
            w, h = aspect_ratio
            ratio_js = f"{w}/{h}" if w > 0 and h > 0 else "null"
        else:
            ratio_js = "null"
        template = _load_embed(tool_id, "extract_garment.html")
        return re.sub(
            r"\{(\w+)\}",
            lambda m: {"src": src, "gallery_attr": gallery_attr, "prompt_attr": prompt_attr, "ratio_js": ratio_js, "source_block": source_block}[m.group(1)],
            template,
        )

    async def extract_garment(
        self,
        image: str,
        garment: str = "upper garment",
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __id__: str = "",
    ):
        """
        Extract (isolate) a garment from a photo and return it as a standalone image.

        image accepts a filename from a previous generation or a direct external image URL. garment is optional and defaults to "upper garment". Returns the extracted image URL as context ({'image': <url>}) for chained tool calls (e.g. virtual_try_on).

        :param image: Filename or URL of the image containing the garment to extract. Required.
        :param garment: Garment to extract. One of: upper garment, lower garment, shirt, t-shirt, jacket, sweater, pullover, pants, skirt, trousers. Default: upper garment.
        """
        if __request__ is None:
            log.error("extract_garment called without request context")
            return "Error: The tool could not be initialized."

        try:
            from open_webui.routers.images import get_image_config

            image_config = await get_image_config()
            user_valves = (__user__ or {}).get("valves", None)

            # =================================================================
            # Garment validation: normalize and match against the supported
            # list. On mismatch, return an actionable error listing the
            # supported types so the model can correct itself and retry.
            # =================================================================
            resolved_garment = _resolve_garment(garment)
            if not resolved_garment:
                return (
                    f"Error: '{garment}' is not a supported garment type. "
                    f"Supported garment types: {', '.join(_SUPPORTED_GARMENTS)}."
                )

            # =================================================================
            # Background color: UserValves > AdminValves > workflow default.
            # The user valve default is "" (unset) on purpose: empty means
            # "inherit from admin" so an admin change propagates to users on
            # the Default setting.
            # =================================================================
            user_background_color = (
                user_valves.background_color
                if user_valves and user_valves.background_color
                else ""
            )
            resolved_background_color = (
                user_background_color
                or self.valves.background_color
                or "#c1ffff"
            )

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

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"\U0001f455 Extracting {resolved_garment}...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            # =================================================================
            # Build the workflow: load from cache and parse
            # =================================================================
            raw_workflow = _load_workflow(__id__, "extract_garment.json")
            workflow = json.loads(raw_workflow)

            # =================================================================
            # Resolve nodes by _meta.title
            # =================================================================
            _, input_node = _resolve_node(workflow, "Load Image (URL/Path)")
            _, florence_node = _resolve_node(workflow, "Florence2Run")
            _, segment_fill_node = _resolve_node(workflow, "Segment Background Fill")
            preview_image_id, _ = _resolve_node(workflow, "Random Preview Image")

            # =================================================================
            # Inject input image — auto-detect URL vs filename
            # =================================================================
            parsed = urlparse(image)
            if parsed.scheme and parsed.netloc:
                input_node["inputs"]["source"] = "url"
                input_node["inputs"]["url"] = image
                input_node["inputs"].pop("image", None)
                input_node["inputs"].pop("Choose file to upload", None)
            elif image.startswith("input:"):
                # Static file in ComfyUI's input/ directory
                input_node["inputs"]["source"] = "input"
                input_node["inputs"]["image"] = image[len("input:"):]
                input_node["inputs"]["url"] = ""
                input_node["inputs"].pop("Choose file to upload", None)
            else:
                input_node["inputs"]["source"] = "temp"
                input_node["inputs"]["image"] = image
                input_node["inputs"]["url"] = ""

            # =================================================================
            # Inject the garment text into Florence-2 (phrase grounding)
            # =================================================================
            florence_node["inputs"]["text_input"] = resolved_garment

            # =================================================================
            # Inject the background color into the segment background fill
            # node (first phase: remove background)
            # =================================================================
            segment_fill_node["inputs"]["background_color"] = resolved_background_color

            log.info(
                "Dispatching extract_garment workflow to ComfyUI (%s) - "
                "image=%s, garment=%s, background_color=%s",
                image_config.COMFYUI_BASE_URL,
                image,
                resolved_garment,
                resolved_background_color,
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

                log.info("Extract garment workflow queued - prompt_id=%s", prompt_id)

                try:
                    outputs = await _comfyui_wait_for_output(
                        client, comfy_base, api_key, prompt_id
                    )
                except asyncio.CancelledError:
                    log.info("Extract garment cancelled by user - interrupting ComfyUI")
                    await _comfyui_interrupt(comfy_base, api_key)
                    raise

            # =================================================================
            # Extract output image filename and build URL
            # =================================================================
            image_filename, image_type = _extract_image_filename(outputs, preview_image_id)

            base = resolved_image_base_url.rstrip("/")
            image_url = f"{base}/api/view?filename={image_filename}&type={image_type}"

            log.info(
                "Extract garment complete - prompt_id=%s, image=%s",
                prompt_id,
                image_filename,
            )

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": f"\u2705 {resolved_garment.capitalize()} extracted.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            # Rich UI embed (see DESIGN.md): the LLM receives only the
            # actionable context ({'image': url}) and never sees the HTML.
            # The output dimensions are not known a priori (the crop follows
            # the garment bbox), so the viewer sizes after the image loads.
            # The result carries the image-viewer gallery markers + the
            # garment as prompt caption, so it is collectible by the
            # conversation gallery and reusable downstream (virtual_try_on).
            # The original input image is shown as a small bottom-left
            # thumbnail in the frame (context of where the garment came from);
            # it is visual only and never part of the LLM context.
            parsed_image = urlparse(image)
            if parsed_image.scheme and parsed_image.netloc:
                original_url = image
            else:
                original_url = f"{base}/api/view?filename={image}&type=temp"

            viewer = self._build_image_viewer(
                image_url,
                aspect_ratio=None,
                gallery=True,
                prompt=resolved_garment,
                original=original_url,
                tool_id=__id__,
            )
            return HTMLResponse(
                content=viewer, headers={"Content-Disposition": "inline"}
            ), {"image": image_url}

        except asyncio.CancelledError:
            log.info("extract_garment cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Garment extraction cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return "The garment extraction was cancelled by the user. Do not retry. Acknowledge the cancellation and wait for their next request."
        except Exception as e:
            log.exception("extract_garment failed: %s", e)
            return f"Error extracting garment: {e}"

"""
title: Compare Images
author: Insecure Erasure
description: Compare two images side by side with an interactive before/after slider
version: 2.8
"""

import html
import logging
import re
from urllib.parse import urlparse

from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)

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



def _build_slider_html(image_a: str, image_b: str, tool_id: str = "") -> str:
    """
    Build the before/after comparison slider embed.
    
    The markup lives in compare_images.html (loaded from the tool's cache
    directory); sizing/fullscreen behavior is documented in the header
    comment of that file and in DESIGN.md §10.
    """
    a = html.escape(image_a, quote=True)
    b = html.escape(image_b, quote=True)
    template = _load_embed(tool_id, "compare_images.html")
    return re.sub(
        r"\{(\w+)\}",
        lambda m: {"a": a, "b": b}[m.group(1)],
        template,
    )


class Tools:
    """
    Compare two images with an interactive before/after slider.

    Only call when the user explicitly asks to compare two images.
    Pass the image URLs returned by the other tools (e.g.
    http://open-webui.private/api/v1/files/<id>/content).
    """

    def __init__(self):
        self.citation = False

    async def compare_images(
        self,
        image_a: str,
        image_b: str,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __id__: str = "",
    ):
        """
        Compare two images with an interactive before/after slider.

        Only call when the user asks to compare two images.

        :param image_a: URL of the first image.
        :param image_b: URL of the second image.
        """
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": "\U0001f5bc\ufe0f Building image comparison...",
                        "done": False,
                        "hidden": False,
                    },
                }
            )

        for name, url in (("image_a", image_a), ("image_b", image_b)):
            parsed = urlparse(url)
            if not (parsed.scheme and parsed.netloc):
                if __event_emitter__:
                    await __event_emitter__(
                        {
                            "type": "status",
                            "data": {
                                "description": "\u274c Invalid image URL.",
                                "done": True,
                                "hidden": False,
                            },
                        }
                    )
                return (
                    f"Error: {name} must be a valid image URL "
                    f"(got {url!r}). Pass the URLs returned by the other tools."
                )

        html_block = _build_slider_html(image_a, image_b)

        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "status",
                    "data": {
                        "description": "\u2705 Comparison ready.",
                        "done": True,
                        "hidden": False,
                    },
                }
            )

        # Bare HTMLResponse (no tuple): this is a terminal result with an
        # empty context, so the LLM receives the middleware's generic
        # "Embedded UI result is active and visible to the user." message
        # instead of any tool-specific context.
        return HTMLResponse(
            content=html_block, headers={"Content-Disposition": "inline"}
        )

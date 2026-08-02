"""
title: Compare Images
author: Insecure Erasure
description: Compare two images side by side with an interactive before/after slider
version: 1.0
"""

from fastapi.responses import HTMLResponse

import html
import logging
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def _build_slider_html(image_a: str, image_b: str) -> str:
    """
    Build the before/after comparison slider as a standalone HTML document.

    The two image URLs are injected into the <img> tags. They are HTML-escaped
    so query strings (e.g. &filename=...&type=...) cannot break the markup.
    The divider starts at 50% so both images are visible on load.
    """
    a = html.escape(image_a, quote=True)
    b = html.escape(image_b, quote=True)

    return f"""<!DOCTYPE html>
<style>
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#222}}
#c{{position:relative;overflow:hidden;cursor:crosshair}}
#c img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}}
#top{{clip-path:inset(0 calc(100% - var(--p,50%)) 0 0)}}
#d{{position:absolute;top:0;bottom:0;left:var(--p,50%);width:2px;background:#fff}}
</style>
<div id="c">
<img src="{a}">
<img id="top" src="{b}">
<div id="d"></div>
</div>
<script>
const c=document.getElementById('c'),im=document.querySelector('#c img');
function fit(){{const r=im.naturalWidth/im.naturalHeight||1;let w=innerWidth,h=w/r;if(h>innerHeight){{h=innerHeight;w=h*r}}c.style.width=w+'px';c.style.height=h+'px'}}
addEventListener('resize',fit);im.addEventListener('load',fit);new ResizeObserver(fit).observe(document.body);
c.addEventListener('mousemove',e=>{{const r=c.getBoundingClientRect(),p=Math.min(100,Math.max(0,(e.clientX-r.left)/r.width*100));c.style.setProperty('--p',p+'%')}});
function reportHeight(){{const h=document.documentElement.scrollHeight;parent.postMessage({{type:'iframe:height',height:h}},'*')}}
addEventListener('load',reportHeight);new ResizeObserver(reportHeight).observe(document.body);
fit();
</script>
"""


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
    ) -> HTMLResponse:
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

        # Rich UI embed: Open WebUI intercepts HTMLResponse with
        # Content-Disposition: inline and renders it as an interactive
        # sandboxed iframe in the chat (docs: Rich UI Embedding).
        return HTMLResponse(
            content=html_block,
            headers={"Content-Disposition": "inline"},
        )

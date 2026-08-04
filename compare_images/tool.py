"""
title: Compare Images
author: Insecure Erasure
description: Compare two images side by side with an interactive before/after slider
version: 2.2
"""

import html
import logging
from urllib.parse import urlparse

from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)


def _build_slider_html(image_a: str, image_b: str) -> str:
    """
    Build the before/after comparison slider as a standalone HTML document.

    The two image URLs are injected into the <img> tags. They are HTML-escaped
    so query strings (e.g. &filename=...&type=...) cannot break the markup.

    The slider fills the full width of the chat container and its height
    follows the aspect ratio of the base image (image_a), with an adaptive
    sizing strategy:

    - Vertical (portrait) devices: full width, no height cap.
    - Horizontal (landscape) devices: height capped at 80%% of the available
      vertical space; the width is scaled proportionally and the slider is
      centered.

    Device orientation is detected inside the sandboxed iframe (it cannot
    read the parent viewport, and its own viewport aspect is misleading):
    screen.orientation when available, then window.orientation, then a
    width/height comparison. Detection is conservative: degenerate screen
    values (e.g. 0-height in some webviews) or any ambiguity are treated as
    portrait so the cap can never fire on a portrait device. The available
    height for the cap is approximated with the device screen
    (screen.availHeight).

    A reportHeight() postMessage keeps the sandboxed iframe height in sync
    with the slider (required by Open WebUI: without it the iframe stays at
    ~150px and the content is cut off). The divider starts at 50%% so both
    images are visible on load.
    """
    a = html.escape(image_a, quote=True)
    b = html.escape(image_b, quote=True)

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{margin:0;background:#222}}
#c{{position:relative;width:100%;margin:0 auto;overflow:hidden;cursor:crosshair}}
#c img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}}
#top{{clip-path:inset(0 calc(100% - var(--p,50%)) 0 0)}}
#d{{position:absolute;top:0;bottom:0;left:var(--p,50%);width:2px;background:#fff;transform:translateX(-50%)}}
</style>
</head>
<body>
<div id="c">
<img src="{a}">
<img id="top" src="{b}">
<div id="d"></div>
</div>
<script>
const c=document.getElementById('c'),im=document.querySelector('#c img');
function reportHeight(){{parent.postMessage({{type:'iframe:height',height:document.documentElement.scrollHeight}},'*')}}
function isLandscape(){{
  if(screen.orientation&&screen.orientation.type)return screen.orientation.type.indexOf('landscape')===0;
  if(typeof window.orientation==='number')return Math.abs(window.orientation)===90;
  const sw=screen.width||0,sh=screen.height||0;
  return sw>sh&&sh>0;
}}
function fit(){{
  const r=(im.naturalWidth||16)/(im.naturalHeight||9);
  // Adaptive sizing: portrait devices use the full width with no height cap;
  // landscape devices cap the height at 80% of the available vertical space
  // and scale the width proportionally (centered). Orientation detection is
  // conservative: any ambiguity (missing orientation API, degenerate screen
  // values) is treated as portrait so the cap can never fire on a portrait
  // device (the reported mobile bug). The available height is approximated
  // with the device screen (availHeight), readable inside the sandbox.
  const maxH=isLandscape()?(screen.availHeight||screen.height||0)*0.8:0;
  let w=document.documentElement.clientWidth;
  if(maxH>0){{const wByH=maxH*r;if(wByH>0&&wByH<w)w=wByH;}}
  c.style.width=w+'px';
  c.style.height=(w/r)+'px';
  reportHeight();
}}
addEventListener('resize',fit);
im.addEventListener('load',fit);
document.getElementById('top').addEventListener('load',fit);
new ResizeObserver(fit).observe(document.body);
c.addEventListener('mousemove',e=>{{const rect=c.getBoundingClientRect(),p=Math.min(100,Math.max(0,(e.clientX-rect.left)/rect.width*100));c.style.setProperty('--p',p+'%')}});
fit();
</script>
</body>
</html>
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

"""
title: Compare Images
author: Insecure Erasure
description: Compare two images side by side with an interactive before/after slider
version: 2.4
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
#c{{position:relative;width:100%;margin:0 auto;overflow:hidden;cursor:crosshair;touch-action:none;user-select:none;-webkit-user-select:none}}
#c img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;-webkit-user-drag:none}}
#top{{clip-path:inset(0 calc(100% - var(--p,50%)) 0 0)}}
#d{{position:absolute;top:0;bottom:0;left:var(--p,50%);width:2px;background:#fff;transform:translateX(-50%);pointer-events:none;box-shadow:0 0 4px rgba(0,0,0,.45)}}
#h{{position:absolute;top:50%;left:var(--p,50%);transform:translate(-50%,-50%);width:32px;height:46px;border-radius:10px;background:rgba(255,255,255,.92);border:1px solid rgba(0,0,0,.18);box-shadow:0 2px 10px rgba(0,0,0,.35);display:flex;align-items:center;justify-content:center;gap:5px;cursor:grab;pointer-events:none}}
#h span{{width:8px;height:8px;border-left:2.5px solid #444;border-bottom:2.5px solid #444;transform:rotate(45deg)}}
#h span:last-child{{transform:rotate(-135deg)}}
</style>
</head>
<body>
<div id="c">
<img src="{a}" draggable="false">
<img id="top" src="{b}" draggable="false">
<div id="d"></div>
<div id="h"><span></span><span></span></div>
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
  // Only size the slider once the base image has real dimensions. The old
  // 16:9 fallback mis-sized the area when the images were not loaded yet,
  // and if the image was already cached the load event never fired to
  // correct it (the reported "reload the frame" fix). Same aspect ratio
  // between both images is assumed; the area follows the base image.
  if(!(im.naturalWidth>0&&im.naturalHeight>0)){{reportHeight();return;}}
  const r=im.naturalWidth/im.naturalHeight;
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
im.addEventListener('load',fit);
document.getElementById('top').addEventListener('load',fit);
window.addEventListener('load',fit);
addEventListener('resize',fit);
new ResizeObserver(fit).observe(document.body);
let dragging=false;
function setP(x){{const rect=c.getBoundingClientRect(),p=Math.min(100,Math.max(0,(x-rect.left)/rect.width*100));c.style.setProperty('--p',p+'%');}}
// Pointer events unify mouse + touch (mobile). Drag anywhere on the slider
// or tap to move the divider. touch-action:none keeps the browser from
// hijacking the gesture for scrolling; the handle (#h) is a purely visual
// affordance (pointer-events:none) and the container is the drag surface.
c.addEventListener('pointerdown',e=>{{dragging=true;try{{c.setPointerCapture(e.pointerId)}}catch{{}}setP(e.clientX);e.preventDefault();}});
c.addEventListener('pointermove',e=>{{if(dragging)setP(e.clientX);}});
c.addEventListener('pointerup',()=>{{dragging=false;}});
c.addEventListener('pointercancel',()=>{{dragging=false;}});
// If the base image was already loaded (e.g. from cache) before this script
// ran, fit() uses its real dimensions immediately; otherwise it reports the
// current height and the load events correct it when the image arrives.
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

"""
title: Virtual Try-On
author: Insecure Erasure
description: Try on an upper and a lower garment on a person photo. Each image argument accepts a filename from a previous generation or a direct external image URL. model_image is required; upper_image and lower_image are optional.
version: 1.0
"""

import asyncio
import html
import json
import logging
import random as _random
import uuid
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)

# =============================================================================
# ComfyUI constants
# =============================================================================
_COMFY_QUEUE_MAX_RETRIES = 600       # ~10 min at 1s intervals (Florence-2 + Flux.2 Klein 9B is slow)
_COMFY_QUEUE_POLL_INTERVAL = 1.0     # seconds
_COMFY_SEED_MAX: int = 1125899906842624

# Default garments served from Open WebUI's static/images/vton/.
# Fixed names, transparent to the user: when a garment is omitted the tool
# falls back to these images.
_DEFAULT_UPPER_FILENAME = "default_upper.png"
_DEFAULT_LOWER_FILENAME = "default_lower.png"


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


def _extract_text(outputs: dict, output_node_id: str) -> str:
    """
    Extract the text from a ShowText|pysssss output node in the workflow history.

    The node exposes the text as a single-element list under the "text" key:
    {"text": ["TRYON A woman. ..."]}.

    Returns the trimmed text string.
    """
    node_output = outputs.get(output_node_id, {})

    # ShowText|pysssss stores the text under "text" as ["prompt"]
    text_list = node_output.get("text")
    if isinstance(text_list, list) and len(text_list) > 0 and isinstance(text_list[0], str):
        return text_list[0].strip()

    # Fallback: raw string under "text"
    text = node_output.get("text")
    if isinstance(text, str):
        return text.strip()

    # Fallback: raw string under "string"
    text = node_output.get("string")
    if isinstance(text, str):
        return text.strip()

    # Worst case: dump the first string value found
    for key, value in node_output.items():
        if isinstance(value, str) and len(value) > 10:
            return value.strip()
        if isinstance(value, list) and len(value) > 0 and isinstance(value[0], str) and len(value[0]) > 10:
            return value[0].strip()

    raise RuntimeError(
        f"Could not extract text from output node {output_node_id}. "
        f"Available outputs: {json.dumps(node_output, indent=2)}"
    )


# =============================================================================
# TOOL
# =============================================================================

class Tools:
    """
    Try on an upper and a lower garment on a person photo.

    Call only when the user asks to try on clothes on a person (virtual
    try-on). Requires a photo of the person to dress. Each image argument
    accepts either a filename from a previous generation or a direct external
    image URL. model_image is required; upper_image and lower_image are
    optional — omit whichever the user did not provide (a missing garment
    falls back to its configured default image). Returns the result as a Rich
    UI embed (image viewer with zoom and download) with context
    {'image': <url>, 'prompt': <text>}: the image URL is actionable for
    chained tool calls, and the prompt (generated by the workflow) is what
    the agent uses to reply to the user.

    Example: model_image='abc123.png', upper_image='https://.../top.jpg',
    lower_image='https://.../skirt.jpg'
    """

    class Valves(BaseModel):
        """Admin-level configuration."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links (overrides COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL.",
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        comfyui_image_base_url: str = Field(
            default="",
            description="Public base URL for image links. Overrides the admin valve and COMFYUI_BASE_URL.",
        )
        seed: int = Field(
            default=-1,
            description="Seed. -1 = random, >=1 = fixed seed for reproducible results.",
        )
        lora_config: str = Field(
            default="[]",
            description='JSON array of extra LoRAs. String=only name (strength 1.0), object={"name"|"model", "strength"}. The workflow try-on LoRA always stays first at strength 1; these are appended after it. Empty name or strength 0 skips the entry. Ex: ["lora1.sft", {"name": "lora2.sft", "strength": 0.5}]',
        )
        prompt_suffix: str = Field(
            default="",
            description="Optional text appended at the end of the generated prompt (after the workflow's default try-on instruction). Leave empty to skip.",
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False


    def _build_image_viewer(self, image_url: str, aspect_ratio: tuple[int, int] | None = None, gallery: bool = False) -> str:
        """
        Build the self-contained image viewer embed for a single image URL.

        The URL is HTML-escaped so query strings (e.g. &filename=...&type=...)
        cannot break the markup.

        Layout: the image is centered, fits the chat container width, and its
        height is capped at 70% of the available screen height (approximation of
        70vh of the real browser viewport, since the iframe's own vh is useless —
        see DESIGN.md §10). If aspect_ratio (reduced_w, reduced_h) is provided,
        the embed reserves that aspect before the image loads to avoid the
        "jump"; otherwise it sizes after the image has real dimensions.

        Clicking the image opens a lightbox that fills the browser window via the
        Fullscreen API (X top-left closes it, download button top-right forces a
        download via fetch blob -> object URL -> anchor). The theme follows
        prefers-color-scheme.

        Gallery: when gallery=True the viewer adds a `data-gallery="1"` marker
        attribute. Opening the lightbox then walks the parent chat DOM
        (same-origin ON — guarded, so same-origin OFF just yields an empty
        gallery) and collects every image in the conversation whose viewer
        carries the marker. The lightbox shows ‹ › buttons (vertically
        centered), a "n/N" counter (bottom-right) and ArrowLeft/ArrowRight
        keyboard navigation with wrap-around (DESIGN.md §11). The download
        button keeps using `big.src`, so it always downloads the image
        currently shown. All gallery logic is JS inside the embed — the marker
        is the only contribution of the tool.

        A failed image load is retried once (no watchdog): on `error` the img
        src is cleared and re-set a single time per URL (flaky/slow fetches),
        after which a second failure is left alone (the browser shows the alt
        text).
        """
        src = html.escape(image_url, quote=True)
        gallery_attr = ' data-gallery="1"' if gallery else ''
        if aspect_ratio:
            w, h = aspect_ratio
            if w > 0 and h > 0:
                ratio_js = f"{w}/{h}"
            else:
                ratio_js = "null"
        else:
            ratio_js = "null"
        return (
f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{
  color-scheme:light dark;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;overflow:hidden;margin:0;padding:0}}
body{{display:flex;align-items:center;justify-content:center;background:transparent}}
img{{-webkit-user-drag:none;user-select:none;-webkit-user-select:none}}
.viewer{{max-width:100%;overflow:hidden;cursor:zoom-in;border-radius:12px}}
.viewer img{{display:block;width:100%;height:100%;object-fit:contain;border-radius:12px}}
.overlay{{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;align-items:center;justify-content:center;z-index:999}}
.overlay.open{{display:flex}}
.overlay img{{max-width:100vw;max-height:100vh;object-fit:contain;box-shadow:0 4px 30px rgba(0,0,0,.5);border-radius:4px}}
.btn{{position:fixed;z-index:1000;display:flex;align-items:center;justify-content:center;background:rgba(28,28,28,.75);border:none;border-radius:8px;color:#f5f5f5;cursor:pointer;padding:6px}}
.btn svg{{display:block}}
.btn.nav{{display:none}}
#close{{top:14px;left:14px}}
#dl{{top:14px;right:14px}}
#prev{{top:50%;left:14px;transform:translateY(-50%)}}
#next{{top:50%;right:14px;transform:translateY(-50%)}}
.counter{{position:fixed;bottom:14px;right:14px;z-index:1000;display:none;align-items:center;justify-content:center;background:rgba(28,28,28,.75);color:#f5f5f5;border-radius:8px;padding:5px 12px;font:600 13px system-ui,sans-serif;pointer-events:none}}
@media (prefers-color-scheme: light){{
  .btn{{background:rgba(235,235,235,.82);color:#1a1a1a}}
  .counter{{background:rgba(235,235,235,.82);color:#1a1a1a}}
}}
</style>
</head>
<body>
<div class="viewer" id="viewer"{gallery_attr}>
  <img id="thumb" src="{src}" alt="Generated image">
</div>
<div class="overlay" id="overlay">
  <img id="big" src="{src}" alt="Generated image">
  <button id="close" class="btn" title="Close" aria-label="Close">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
  </button>
  <button id="dl" class="btn" title="Download" aria-label="Download">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
  </button>
  <button id="prev" class="btn nav" title="Previous image" aria-label="Previous image">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M15 18l-6-6 6-6"/></svg>
  </button>
  <button id="next" class="btn nav" title="Next image" aria-label="Next image">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>
  </button>
  <div id="counter" class="counter"></div>
</div>
<script>
const viewer=document.getElementById('viewer'),thumb=document.getElementById('thumb'),
      overlay=document.getElementById('overlay'),big=document.getElementById('big'),
      closeBtn=document.getElementById('close'),dlBtn=document.getElementById('dl'),
      prevBtn=document.getElementById('prev'),nextBtn=document.getElementById('next'),
      counter=document.getElementById('counter');
const RESERVED_R={ratio_js};
function reportHeight(){{parent.postMessage({{type:'iframe:height',height:viewer.offsetHeight||document.documentElement.scrollHeight}},'*')}}
function fit(){{
  // Sizing replicates the compare_images slider (DESIGN.md §10): the iframe
  // starts at ~150px and its own vh is useless, so derive from the container
  // width and the image aspect ratio, cap the height at 70% of the available
  // screen height (screen.availHeight), and report the resulting height.
  // While the lightbox is in browser fullscreen the iframe viewport is the
  // full screen and resizing/reporting would blow up the embed and shift the
  // chat scroll — so skip sizing during fullscreen entirely (the overlay
  // covers everything anyway) and re-fit when fullscreen ends.
  if(document.fullscreenElement||document.webkitFullscreenElement)return;
  let r=0;
  if(RESERVED_R)r=Number(RESERVED_R);
  if(!(r>0)&&thumb.naturalWidth>0&&thumb.naturalHeight>0)r=thumb.naturalWidth/thumb.naturalHeight;
  if(!(r>0)){{reportHeight();return;}}
  const maxH=(screen.availHeight||screen.height||0)*0.7;
  let w=document.documentElement.clientWidth;
  if(maxH>0){{const wByH=maxH*r;if(wByH>0&&wByH<w)w=wByH;}}
  viewer.style.width=w+'px';
  viewer.style.height=(w/r)+'px';
  reportHeight();
}}
thumb.addEventListener('load',fit);
big.addEventListener('load',fit);
window.addEventListener('load',fit);
addEventListener('resize',fit);
new ResizeObserver(fit).observe(document.body);
fit();
// Failed-image retry (cheap fix, NO watchdog — maintainer decision,
// 2026-08-04, DESIGN.md §11): a slow/flaky fetch can leave the embed
// without the image (no 'load' event → the viewer never sizes; reloading
// the frame re-runs the script). On 'error' we clear and re-set the src a
// single time per URL; if the retry also fails we leave it alone (the
// browser shows the alt text).
let retriedSrc=null;
function retryOnce(img){{
  if(retriedSrc===img.src)return;
  retriedSrc=img.src;
  const s=img.src;
  img.removeAttribute('src');
  requestAnimationFrame(()=>{{img.src=s;}});
}}
thumb.addEventListener('error',()=>retryOnce(thumb));
big.addEventListener('error',()=>retryOnce(big));
// Gallery (DESIGN.md §11): collect every image in the chat whose viewer
// carries the data-gallery marker. The marker is the ONLY contribution of the
// tool — the collection logic lives here, in the embed (maintainer
// constraint: no backend/Python gallery logic in the tools). Requires
// same-origin access to the parent chat DOM (the user's Open WebUI has it
// ON); with same-origin OFF every contentDocument is null, the gallery stays
// empty and the lightbox behaves exactly as before.
let gallery=[],galleryIdx=-1;
function collectGallery(){{
  gallery=[];galleryIdx=-1;
  try{{
    const frames=parent.document.querySelectorAll('iframe');
    for(let i=0;i<frames.length;i++){{
      let cd=null;
      try{{cd=frames[i].contentDocument;}}catch(e){{}}
      if(!cd)continue;
      const v=cd.querySelector('.viewer[data-gallery]');
      if(!v)continue;
      // Read the THUMBNAIL src, not the lightbox img: big.src is mutated by
      // gallery navigation, so after navigating once an embed's big no longer
      // reflects its own image (its image would drop out and duplicates would
      // appear). thumb.src is the stable per-embed identity.
      const im=cd.getElementById('thumb');
      if(im&&im.src&&gallery.indexOf(im.src)<0)gallery.push(im.src);
    }}
  }}catch(e){{}}
  // Defensive: after the reset below big.src === thumb.src and this embed's
  // own thumb is normally collected (its iframe is in the parent DOM); if for
  // any reason it was not collected, unshift it so the current view is present.
  if(gallery.indexOf(big.src)<0)gallery.unshift(big.src);
  galleryIdx=gallery.indexOf(big.src);
  const multi=gallery.length>1;
  prevBtn.style.display=nextBtn.style.display=counter.style.display=multi?'flex':'none';
  if(multi&&galleryIdx>=0)counter.textContent=(galleryIdx+1)+'/'+gallery.length;
}}
function showImage(i){{
  // Wrap-around navigation; a no-op when there is only one image.
  if(gallery.length<2)return;
  galleryIdx=((i%gallery.length)+gallery.length)%gallery.length;
  big.src=gallery[galleryIdx];
  counter.textContent=(galleryIdx+1)+'/'+gallery.length;
}}
function openLightbox(){{
  // Start from this embed's own image: big.src may be left pointing at a
  // gallery-navigated URL from a previous open — reset it so the view, the
  // counter index and the download are consistent with the thumbnail.
  big.src=thumb.src;
  collectGallery();
  overlay.classList.add('open');
  // Fullscreen the OVERLAY element, not the documentElement: the overlay is
  // already position:fixed inset:0, so the browser expands it to the window
  // WITHOUT scrolling the parent chat to the top (the known scroll jump when
  // fullscreening documentElement). Exiting also leaves the parent scroll
  // untouched.
  try{{overlay.requestFullscreen&&overlay.requestFullscreen();}}catch(e){{}}
  try{{overlay.webkitRequestFullscreen&&overlay.webkitRequestFullscreen();}}catch(e){{}}
}}
function closeLightbox(){{
  // If in fullscreen, exit it; the overlay is removed and the size re-fit in
  // the fullscreenchange handler (avoids a flash of the bare viewer fullscreen).
  if(document.fullscreenElement||document.webkitFullscreenElement){{
    try{{document.exitFullscreen&&document.exitFullscreen();}}catch(e){{}}
    try{{document.webkitExitFullscreen&&document.webkitExitFullscreen();}}catch(e){{}}
  }}else{{
    overlay.classList.remove('open');
    restoreScroll();
  }}
}}
// Restore the scroll position after closing the lightbox. The chat scroll is
// NOT on the parent window (parent.scrollY stays 0); it lives in an inner
// scroll container inside Open WebUI's DOM. With allow-same-origin ON we can
// walk the parent document, find the scrolled elements, and restore their
// scrollTop. Save the scroll of the parent window AND of all inner scrolled
// containers before opening, restore both after closing.
let savedScrolls=[];
function saveScroll(){{
  savedScrolls=[];
  try{{savedScrolls.push({{el:parent,top:parent.scrollY||0}});}}catch(e){{}}
  try{{
    const doc=parent.document||document;
    const all=doc.querySelectorAll&&doc.querySelectorAll('*');
    if(all)for(let i=0;i<all.length;i++){{
      const el=all[i];
      if(el.scrollTop>0&&el.scrollHeight>el.clientHeight)savedScrolls.push({{el:el,top:el.scrollTop}});
    }}
  }}catch(e){{}}
}}
function restoreScroll(){{requestAnimationFrame(()=>{{requestAnimationFrame(()=>{{
  try{{parent.scrollTo(0,savedScrolls[0]&&savedScrolls[0].top||0);}}catch(e){{}}
  for(let i=0;i<savedScrolls.length;i++){{try{{savedScrolls[i].el.scrollTop=savedScrolls[i].top;}}catch(e){{}}}}
  document.documentElement.scrollTop=0;document.body.scrollTop=0;
}});}});}}
viewer.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;saveScroll();openLightbox();}});
closeBtn.addEventListener('pointerup',()=>{{closeLightbox();restoreScroll();}});
overlay.addEventListener('pointerup',e=>{{if(e.target===overlay){{closeLightbox();restoreScroll();}}}});
big.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;e.stopPropagation();}});
prevBtn.addEventListener('pointerup',e=>{{e.stopPropagation();showImage(galleryIdx-1);}});
nextBtn.addEventListener('pointerup',e=>{{e.stopPropagation();showImage(galleryIdx+1);}});
document.addEventListener('keydown',e=>{{
  if(e.key==='Escape'){{closeLightbox();restoreScroll();}}
  else if(e.key==='ArrowLeft'){{if(overlay.classList.contains('open'))showImage(galleryIdx-1);}}
  else if(e.key==='ArrowRight'){{if(overlay.classList.contains('open'))showImage(galleryIdx+1);}}
}});
document.addEventListener('fullscreenchange',()=>{{if(!(document.fullscreenElement||document.webkitFullscreenElement)){{overlay.classList.remove('open');restoreScroll();fit();}}}});
async function download(){{try{{const r=await fetch(big.src);if(!r.ok)throw new Error('HTTP '+r.status);const b=await r.blob();const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download='image.png';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),1000);}}catch(err){{const w=window.open(big.src,'_blank');if(w)w.focus();}}}}
dlBtn.addEventListener('pointerup',download);
</script>
</body>
</html>
"""
        )

    async def virtual_try_on(
        self,
        model_image: str,
        upper_image: str = "",
        lower_image: str = "",
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __id__: str = "",
    ):
        """
        Try on an upper and a lower garment on a person photo.

        Call only when the user explicitly asks to try on clothes on a person.
        model_image is required; upper_image and lower_image are optional —
        omit whichever the user did not provide. Each image argument is a
        string accepting either a filename from a previous generation (e.g.
        'abc123.png') or a direct external image URL (e.g. 'https://...').

        :param model_image: Filename or URL of the person photo to dress. Required.
        :param upper_image: Filename or URL of the upper garment photo (top, jacket, shirt...). Optional.
        :param lower_image: Filename or URL of the lower garment photo (trousers, skirt, shorts...). Optional.
        """
        if __request__ is None:
            log.error("virtual_try_on called without request context")
            return "Error: The tool could not be initialized."

        try:
            from open_webui.routers.images import get_image_config

            image_config = await get_image_config()
            user_valves = (__user__ or {}).get("valves", None)

            # =================================================================
            # Garment resolution & fallbacks
            #   Both garments are optional. A missing upper/lower garment falls
            #   back to the fixed default images in static/images/vton/
            #   (default_upper.png / default_lower.png). When a default is used
            #   a notification is emitted so the user knows.
            # =================================================================
            default_upper = _DEFAULT_UPPER_FILENAME
            default_lower = _DEFAULT_LOWER_FILENAME

            # =================================================================
            # Default garments live in Open WebUI's static/images/vton/ and are
            # referenced by filename. Build the full URL from the Open WebUI
            # base URL, resolved the same way Open WebUI itself does for its
            # own links (OAuth redirects, share URLs): global config
            # 'webui.url' first, falling back to the request's base URL.
            # =================================================================
            from open_webui.models.config import Config

            webui_url = await Config.get('webui.url')
            owui_base = (str(webui_url or __request__.base_url)).rstrip('/')

            def _resolve_default_image(value: str) -> str:
                """Resolve a default garment reference to something the
                LoadImageByUrlOrPath node understands.

                - 'input:foo.png'  -> passed through (ComfyUI input/)
                - full URL         -> passed through
                - 'foo.png'        -> Open WebUI static URL:
                                     <owui_base>/static/images/vton/foo.png
                """
                if not value:
                    return ""
                parsed = urlparse(value)
                if parsed.scheme and parsed.netloc:
                    return value
                if value.startswith("input:"):
                    return value
                return f"{owui_base}/static/images/vton/{value.lstrip('/')}"

            resolved_upper = upper_image or _resolve_default_image(default_upper)
            resolved_lower = lower_image or _resolve_default_image(default_lower)

            # =================================================================
            # Salvaguard: check the default garment files exist on disk before
            # dispatching. Only checked when the default is actually going to
            # be used (i.e. the user did not provide that garment). Otherwise
            # ComfyUI would fail later with an obscure image-load error.
            # =================================================================
            from open_webui.env import STATIC_DIR

            def _default_file_exists(filename: str) -> bool:
                return (STATIC_DIR / "images" / "vton" / filename).is_file()

            if not upper_image and not _default_file_exists(default_upper):
                return (
                    "Error: Default upper garment file not found: "
                    f"{STATIC_DIR / 'images' / 'vton' / default_upper}. "
                    "Create it or provide an upper garment image."
                )
            if not lower_image and not _default_file_exists(default_lower):
                return (
                    "Error: Default lower garment file not found: "
                    f"{STATIC_DIR / 'images' / 'vton' / default_lower}. "
                    "Create it or provide a lower garment image."
                )

            missing_garments = []
            if not upper_image:
                missing_garments.append("upper")
            if not lower_image:
                missing_garments.append("lower")

            # =================================================================
            # Build the workflow: load from cache and parse
            # =================================================================
            raw_workflow = _load_workflow(__id__, "virtual_try_on.json")
            workflow = json.loads(raw_workflow)

            # =================================================================
            # Resolve nodes by _meta.title
            # =================================================================
            _, model_node = _resolve_node(workflow, "Model")
            _, upper_node = _resolve_node(workflow, "Upper garments")
            _, lower_node = _resolve_node(workflow, "Lower garments")
            _, random_noise = _resolve_node(workflow, "RandomNoise")
            _, lora_node = _resolve_node(workflow, "Power Lora Loader (rgthree)")
            _, suffix_node = _resolve_node(workflow, "Prompt suffix")
            preview_image_id, _ = _resolve_node(workflow, "Random Preview Image")
            prompt_node_id, _ = _resolve_node(workflow, "Prompt preview")

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

            # =================================================================
            # LoRAs — same JSON format as Smart Generate Image, but with no
            # admin valve. The workflow's try-on LoRA is ALWAYS slot 1 at
            # strength 1; the user's extra LoRAs are appended after it.
            # Entries with an empty name, strength 0, or a name matching the
            # try-on LoRA are skipped (avoids duplicates).
            # =================================================================
            tryon_slot = lora_node["inputs"].get("lora_1")
            tryon_name = tryon_slot.get("lora", "") if isinstance(tryon_slot, dict) else ""

            user_loras = []
            raw_lora_config = (
                user_valves.lora_config if user_valves and user_valves.lora_config else ""
            )
            if raw_lora_config and raw_lora_config.strip():
                try:
                    parsed_config = json.loads(raw_lora_config)
                except json.JSONDecodeError as e:
                    return f"Error: Invalid JSON in lora_config: {e}"
                if not isinstance(parsed_config, list):
                    return (
                        f"Error: lora_config must be a JSON array, got "
                        f"{type(parsed_config).__name__}. Ex: [\"lora.sft\"]"
                    )
                for i, item in enumerate(parsed_config):
                    if isinstance(item, str):
                        user_loras.append(item)
                    elif isinstance(item, dict):
                        name = item.get("name", item.get("model", None))
                        if name is not None and not isinstance(name, str):
                            return (
                                f"Error: lora_config[{i}] 'name'/'model' must be a "
                                f"string, got {type(name).__name__}"
                            )
                        strength = item.get("strength", None)
                        if strength is not None and not isinstance(strength, (int, float)):
                            return (
                                f"Error: lora_config[{i}] 'strength' must be a "
                                f"number, got {type(strength).__name__}"
                            )
                        if name:
                            user_loras.append(item)
                    else:
                        return (
                            f"Error: lora_config[{i}] must be a string or object, "
                            f"got {type(item).__name__}"
                        )

            def _lora_name(item):
                if isinstance(item, str):
                    return item
                if isinstance(item, dict):
                    return item.get("name", item.get("model", ""))
                return ""

            # Normalize every LoRA to (name, strength) and filter skips
            combined = []
            if tryon_name:
                combined.append((tryon_name, 1.0))
            for item in user_loras:
                name = _lora_name(item)
                if not name:
                    continue
                if name == tryon_name:
                    continue  # already fixed in slot 1
                strength = (
                    float(item.get("strength", 1.0)) if isinstance(item, dict) else 1.0
                )
                if strength == 0:
                    continue
                combined.append((name, strength))

            max_slots = sum(1 for k in lora_node["inputs"] if k.startswith("lora_"))
            combined = combined[:max_slots]

            log.info(
                "LoRA injection: tryon=%s user_raw=%s combined=%s",
                tryon_name,
                raw_lora_config,
                json.dumps(combined),
            )

            for i, (name, strength) in enumerate(combined, start=1):
                slot = f"lora_{i}"
                if slot not in lora_node["inputs"]:
                    break
                if bool(name) and strength != 0:
                    lora_node["inputs"][slot]["on"] = True
                    lora_node["inputs"][slot]["lora"] = name
                    lora_node["inputs"][slot]["strength"] = strength
                else:
                    lora_node["inputs"][slot]["on"] = False
                    lora_node["inputs"][slot]["lora"] = ""
                    lora_node["inputs"][slot]["strength"] = 0

            # Turn off any slots left over after the combined list
            for i in range(len(combined) + 1, max_slots + 1):
                slot = f"lora_{i}"
                if slot in lora_node["inputs"]:
                    lora_node["inputs"][slot]["on"] = False
                    lora_node["inputs"][slot]["lora"] = ""
                    lora_node["inputs"][slot]["strength"] = 0

            # Single-line status (event emitter doesn't support multi-line).
            # "with extra LoRAs" only when the user added LoRAs beyond the
            # workflow's fixed try-on LoRA. Default-garment usage is appended
            # as a status message (event emitter), not a toast.
            has_extra_loras = len(combined) > 1

            if __event_emitter__:
                status_desc = "\U0001f455 Running virtual try-on"
                if has_extra_loras:
                    status_desc += " with extra LoRAs"
                if missing_garments:
                    if len(missing_garments) == 2:
                        status_desc += " using default upper and lower garments"
                    else:
                        g = missing_garments[0]
                        status_desc += f" using default {g} garment"
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
            # Configure image sources — auto-detect URL vs filename for each
            # =================================================================
            def _set_image_source(node_inputs: dict, image: str) -> None:
                parsed = urlparse(image)
                if parsed.scheme and parsed.netloc:
                    node_inputs["source"] = "url"
                    node_inputs["url"] = image
                    node_inputs.pop("image", None)
                    node_inputs.pop("Choose file to upload", None)
                elif image.startswith("input:"):
                    # Static file in ComfyUI's input/ directory
                    node_inputs["source"] = "input"
                    node_inputs["image"] = image[len("input:"):]
                    node_inputs["url"] = ""
                    node_inputs.pop("Choose file to upload", None)
                else:
                    node_inputs["source"] = "temp"
                    node_inputs["image"] = image
                    node_inputs["url"] = ""

            _set_image_source(model_node["inputs"], model_image)
            _set_image_source(upper_node["inputs"], resolved_upper)
            _set_image_source(lower_node["inputs"], resolved_lower)

            # =================================================================
            # Prompt suffix: user text appended to the generated prompt
            # (string_b of the "Prompt suffix" concat node)
            # =================================================================
            user_prompt_suffix = (
                user_valves.prompt_suffix
                if user_valves and user_valves.prompt_suffix
                else ""
            )
            suffix_node["inputs"]["string_b"] = user_prompt_suffix

            # =================================================================
            # Seed: UserValve. -1 = random, >=1 = fixed
            # =================================================================
            user_seed = int(user_valves.seed) if user_valves and user_valves.seed != -1 else -1
            seed_arg = (
                _random.randint(1, _COMFY_SEED_MAX)
                if user_seed == -1
                else min(max(user_seed, 1), _COMFY_SEED_MAX)
            )
            random_noise["inputs"]["noise_seed"] = seed_arg

            log.info(
                "Dispatching virtual try-on workflow to ComfyUI (%s) - "
                "model=%s, upper=%s, lower=%s, seed=%d, loras=%s, prompt_suffix=%s",
                image_config.COMFYUI_BASE_URL,
                model_image,
                resolved_upper,
                resolved_lower,
                seed_arg,
                json.dumps(combined) if combined else "(none)",
                user_prompt_suffix or "(none)",
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

                log.info("Virtual try-on workflow queued - prompt_id=%s", prompt_id)

                try:
                    outputs = await _comfyui_wait_for_output(
                        client, comfy_base, api_key, prompt_id
                    )
                except asyncio.CancelledError:
                    log.info("Virtual try-on cancelled by user - interrupting ComfyUI")
                    await _comfyui_interrupt(comfy_base, api_key)
                    raise

            # =================================================================
            # Extract output image filename and build URL
            # =================================================================
            image_filename, image_type = _extract_image_filename(outputs, preview_image_id)

            base = resolved_image_base_url.rstrip("/")
            image_url = f"{base}/api/view?filename={image_filename}&type={image_type}"

            # =================================================================
            # Extract the prompt generated by the workflow
            # =================================================================
            prompt = _extract_text(outputs, prompt_node_id)

            log.info(
                "Virtual try-on complete - prompt_id=%s, image=%s, prompt_len=%d",
                prompt_id,
                image_filename,
                len(prompt),
            )

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2705 Virtual try-on complete.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            # Rich UI embed (see DESIGN.md): the LLM receives only the
            # actionable context ({'image': url, 'prompt': text}) and never
            # sees the HTML. The 'prompt' is the only justified exception to
            # the minimal context: it is the prompt generated by the workflow,
            # which the agent uses to reply to the user. The output dimensions
            # are unknown a priori, so the viewer sizes after the image loads.
            viewer = self._build_image_viewer(image_url, gallery=True)
            return HTMLResponse(
                content=viewer, headers={"Content-Disposition": "inline"}
            ), {"image": image_url, "prompt": prompt}

        except asyncio.CancelledError:
            log.info("virtual_try_on cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Virtual try-on cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return (
                "The virtual try-on was cancelled by the user. "
                "Do not retry. Acknowledge the cancellation and wait for their next request."
            )
        except Exception as e:
            log.exception("virtual_try_on failed: %s", e)
            return f"Error running virtual try-on: {e}"

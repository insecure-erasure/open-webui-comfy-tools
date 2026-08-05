"""
title: Upscale Image
author: Insecure Erasure
description: Upscale a previously generated image using SeedVR2
version: 1.1
"""

import asyncio
import html
import json
import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)


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


_COMFY_QUEUE_TIMEOUT = 60           # seconds


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
    Upscale a previously generated image.

    Only call when the user explicitly asks to upscale
    an image. Pass an image filename or an URL.

    image: The filename previously generated from the smart_generate_image
        response, or a direct URL to an external image.
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

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False


    def _build_image_viewer(self, image_url: str, aspect_ratio: tuple[int, int] | None = None, gallery: bool = False, prompt: str | None = None) -> str:
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

        Prompt caption: when prompt is provided it is added as a `data-prompt`
        attribute (HTML-escaped) — another HTML identifier, never backend
        logic. In the lightbox only (never the thumbnail), a gradient overlay
        at the bottom shows the prompt in white: the gradient goes from
        transparent at the top to dark at the bottom so the white text (in the
        darkest zone) stays readable over any image content; a subtle
        text-shadow reinforces it. When the gallery navigates, the caption
        follows the shown image's prompt.

        A failed image load is retried once (no watchdog): on `error` the img
        src is cleared and re-set a single time per URL (flaky/slow fetches),
        after which a second failure is left alone (the browser shows the alt
        text).
        """
        src = html.escape(image_url, quote=True)
        gallery_attr = ' data-gallery="1"' if gallery else ''
        prompt_attr = f' data-prompt="{html.escape(prompt, quote=True)}"' if prompt else ''
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
.caption{{position:absolute;left:0;right:0;bottom:0;display:none;padding:56px 24px 18px;color:#fff;text-align:center;font:500 15px/1.5 system-ui,sans-serif;text-shadow:0 1px 4px rgba(0,0,0,.75);white-space:pre-wrap;overflow:hidden;pointer-events:none;background:linear-gradient(to bottom,rgba(0,0,0,0) 0%,rgba(0,0,0,.3) 30%,rgba(0,0,0,.65) 65%,rgba(0,0,0,.88) 100%)}}
.caption.show{{display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical}}
@media (prefers-color-scheme: light){{
  .btn{{background:rgba(235,235,235,.82);color:#1a1a1a}}
  .counter{{background:rgba(235,235,235,.82);color:#1a1a1a}}
}}
</style>
</head>
<body>
<div class="viewer" id="viewer"{gallery_attr}{prompt_attr}>
  <img id="thumb" src="{src}" alt="Generated image">
</div>
<div class="overlay" id="overlay">
  <img id="big" src="{src}" alt="Generated image">
  <div id="caption" class="caption"></div>
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
      counter=document.getElementById('counter'),caption=document.getElementById('caption');
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
function updateCaption(){{
  // Prompt caption of the currently shown image (DESIGN.md §12): only in the
  // lightbox, never the thumbnail. The gradient goes from transparent at the
  // top to dark at the bottom so the white text (in the darkest zone) stays
  // readable over any image content. textContent (never innerHTML) — the
  // prompt is arbitrary user/LLM text.
  const p=(gallery[galleryIdx]&&gallery[galleryIdx].prompt)||'';
  if(p){{caption.textContent=p;caption.classList.add('show');}}
  else{{caption.classList.remove('show');caption.textContent='';}}
}}
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
      // data-prompt is the prompt of that embed's image, shown when the
      // gallery navigates to it.
      const pr=v.getAttribute('data-prompt')||'';
      if(im&&im.src&&!gallery.some(g=>g.src===im.src))gallery.push({{src:im.src,prompt:pr}});
    }}
  }}catch(e){{}}
  // Defensive: after the reset below big.src === thumb.src and this embed's
  // own thumb is normally collected (its iframe is in the parent DOM); if for
  // any reason it was not collected, unshift it so the current view is present.
  if(!gallery.some(g=>g.src===big.src))gallery.unshift({{src:big.src,prompt:viewer.getAttribute('data-prompt')||''}});
  galleryIdx=gallery.findIndex(g=>g.src===big.src);
  const multi=gallery.length>1;
  prevBtn.style.display=nextBtn.style.display=counter.style.display=multi?'flex':'none';
  if(multi&&galleryIdx>=0)counter.textContent=(galleryIdx+1)+'/'+gallery.length;
  updateCaption();
}}
function showImage(i){{
  // Wrap-around navigation; a no-op when there is only one image.
  if(gallery.length<2)return;
  galleryIdx=((i%gallery.length)+gallery.length)%gallery.length;
  big.src=gallery[galleryIdx].src;
  counter.textContent=(galleryIdx+1)+'/'+gallery.length;
  updateCaption();
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

    async def upscale(
        self,
        image: str,
        __request__=None,
        __user__=None,
        __event_emitter__=None,
        __chat_id__=None,
        __message_id__=None,
        __id__: str = "",
    ):
        """
        Upscale a previously generated image.

        Only call when the user explicitly asks to upscale
        an image. Pass an image filename or an URL.

        The upscaled image is displayed in the chat as a Rich UI embed
        (image viewer with zoom and download). The tool returns the image
        URL as context ({'image': <url>}); use it for chained tool calls
        or to refer to the upscaled image.

        :param image: The filename previously generated from the smart_generate_image response, or a direct URL to an external image.
        """
        if __request__ is None:
            log.error("upscale called without request context")
            return "Error: The tool could not be initialized."

        try:
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\U0001f52e Upscaling image...",
                            "done": False,
                            "hidden": False,
                        },
                    }
                )

            from open_webui.routers.images import get_image_config

            image_config = await get_image_config()

            # =================================================================
            # Resolve image base URL for the output link
            #   UserValves > AdminValves > COMFYUI_BASE_URL
            # =================================================================
            user_valves = (__user__ or {}).get("valves", None)
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
            # Build the workflow: load from cache and parse
            # =================================================================
            raw_workflow = _load_workflow(__id__, "upscale_image.json")
            workflow = json.loads(raw_workflow)

            # Configure image source — auto-detect URL vs filename
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

            log.info(
                "Dispatching upscale workflow to ComfyUI (%s) - %s=%s",
                image_config.COMFYUI_BASE_URL,
                "url" if parsed.scheme and parsed.netloc else "file",
                image,
            )

            # Resolve the preview node for output extraction
            preview_image_id, _ = _resolve_node(workflow, "Random Preview Image")

            # =================================================================
            # Execute workflow via ComfyUI API
            # =================================================================
            comfy_base = image_config.COMFYUI_BASE_URL.rstrip("/")
            api_key = image_config.COMFYUI_API_KEY or ""

            async with httpx.AsyncClient() as client:
                prompt_id = await _comfyui_queue_prompt(
                    client, comfy_base, api_key, workflow
                )

                log.info("Upscale workflow queued - prompt_id=%s", prompt_id)

                try:
                    outputs = await _comfyui_wait_for_output(
                        client, comfy_base, api_key, prompt_id
                    )
                except asyncio.CancelledError:
                    log.info("Upscale cancelled by user - interrupting ComfyUI")
                    await _comfyui_interrupt(comfy_base, api_key)
                    raise

            # =================================================================
            # Extract image filename and build URL
            # =================================================================
            upscaled_filename, image_type = _extract_image_filename(outputs, preview_image_id)

            base = resolved_image_base_url.rstrip("/")
            upscaled_url = f"{base}/api/view?filename={upscaled_filename}&type={image_type}"

            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2705 Image upscaled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )

            # Rich UI embed (see DESIGN.md): the LLM receives only the
            # actionable context ({'image': url}) and never sees the HTML.
            # The output dimensions are unknown a priori (they depend on the
            # input image), so the viewer sizes itself after the image loads
            # (no aspect reservation). The URL (not the filename) is emitted
            # so downstream tools and compare_images can use it directly.
            viewer = self._build_image_viewer(upscaled_url, gallery=True)
            return HTMLResponse(
                content=viewer, headers={"Content-Disposition": "inline"}
            ), {"image": upscaled_url}

        except asyncio.CancelledError:
            log.info("upscale cancelled by user")
            if __event_emitter__:
                await __event_emitter__(
                    {
                        "type": "status",
                        "data": {
                            "description": "\u2753 Image upscaling cancelled.",
                            "done": True,
                            "hidden": False,
                        },
                    }
                )
            return (
                "The image upscaling was cancelled by the user. "
                "Do not retry. Acknowledge the cancellation and wait for their next request."
            )
        except Exception as e:
            log.exception("upscale failed: %s", e)
            return f"Error upscaling image: {e}"

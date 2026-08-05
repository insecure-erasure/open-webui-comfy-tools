"""
title: Virtual Try-On
author: Insecure Erasure
description: Try on an upper and a lower garment on a person photo. Each image argument accepts a filename from a previous generation or a direct external image URL. model_image is required; upper_image and lower_image are optional.
version: 1.1
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


    def _build_compare_slider(
        self,
        image_a: str,
        image_b: str,
        gallery: bool = False,
        prompt: str | None = None,
    ) -> str:
        """
        Build the before/after comparison slider as a standalone HTML document.

        The same embed as compare_images/upscale_image (DESIGN.md §10): the
        preview IS the interactive divider slider (original model photo vs
        try-on result), and a floating maximize button (bottom-right) opens
        the fullscreen overlay with its OWN interactive slider. NO gallery
        navigation: the fullscreen only shows the comparison (plus the prompt
        caption, a plain text overlay, and the exit button).

        The two image URLs are injected into the <img> tags. They are
        HTML-escaped so query strings (e.g. &filename=...&type=...) cannot
        break the markup.

        Download buttons (maintainer request, 2026-08-05): a download button
        sits at the top-right of the embed (vertically above the fullscreen
        button) and another at the top-right of the fullscreen overlay. Both
        fetch the result image as a blob and force a download (fetch -> blob
        -> object URL -> anchor); on failure (e.g. iOS sandboxed iframe) they
        open the image in a new tab.

        Gallery collection markers (maintainer request, 2026-08-05): when
        gallery=True the container carries `class="viewer"` +
        `data-gallery="1"` and the result <img> gets `id="thumb"` with a
        `data-prompt` — the SAME markers the image viewer uses. This makes the
        result collectible by the conversation gallery that the OTHER viewer
        embed (smart_generate_image) opens — it appears there with its
        generated prompt. This slider itself does NOT navigate the gallery: it
        only shows the before/after comparison.

        Sizing: same as compare_images — portrait full width no cap, landscape
        capped at 80% of the available screen height; both images share the
        aspect ratio (the workflow derives the latent size from the model
        photo).
        """
        a = html.escape(image_a, quote=True)
        b = html.escape(image_b, quote=True)
        gallery_attr = ' class="viewer" data-gallery="1"' if gallery else ''
        prompt_attr = (
            f' data-prompt="{html.escape(prompt, quote=True)}"'
            if prompt
            else ''
        )
        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{margin:0;background:#222}}
html,body{{height:100%;overflow:hidden;margin:0;padding:0}}
#c{{position:relative;width:100%;margin:0 auto;overflow:hidden;cursor:crosshair;touch-action:none;user-select:none;-webkit-user-select:none}}
#c img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;-webkit-user-drag:none}}
#thumb{{clip-path:inset(0 calc(100% - var(--p,50%)) 0 0)}}
#d{{position:absolute;top:0;bottom:0;left:var(--p,50%);width:2px;background:rgba(255,255,255,.75);transform:translateX(-50%);pointer-events:none;mix-blend-mode:difference}}
#h{{position:absolute;top:50%;left:var(--p,50%);transform:translate(-50%,-50%);width:13px;height:18px;border-radius:4px;background:#fff;border:1px solid #333;box-shadow:0 1px 4px rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;gap:2px;cursor:grab;pointer-events:none}}
#h span{{width:3px;height:3px;border-left:1px solid #444;border-bottom:1px solid #444;transform:rotate(45deg)}}
#h span:last-child{{transform:rotate(-135deg)}}
.btn{{position:absolute;display:flex;align-items:center;justify-content:center;background:rgba(28,28,28,.75);border:none;border-radius:8px;color:#f5f5f5;cursor:pointer;padding:6px;z-index:5}}
.btn svg{{display:block}}
#fs{{bottom:8px;right:8px}}
#dl{{top:8px;right:8px}}
@media (prefers-color-scheme: light){{
  .btn{{background:rgba(235,235,235,.82);color:#1a1a1a}}
}}
.overlay{{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:999}}
.overlay.open{{display:flex}}
#c2{{position:relative;overflow:hidden;cursor:crosshair;touch-action:none;user-select:none;-webkit-user-select:none;box-shadow:0 4px 30px rgba(0,0,0,.5);border-radius:4px}}
#c2 img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;-webkit-user-drag:none}}
#top2{{clip-path:inset(0 calc(100% - var(--p,50%)) 0 0)}}
#d2{{position:absolute;top:0;bottom:0;left:var(--p,50%);width:2px;background:rgba(255,255,255,.75);transform:translateX(-50%);pointer-events:none;mix-blend-mode:difference}}
#h2{{position:absolute;top:50%;left:var(--p,50%);transform:translate(-50%,-50%);width:13px;height:18px;border-radius:4px;background:#fff;border:1px solid #333;box-shadow:0 1px 4px rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;gap:2px;cursor:grab;pointer-events:none}}
#h2 span{{width:3px;height:3px;border-left:1px solid #444;border-bottom:1px solid #444;transform:rotate(45deg)}}
#h2 span:last-child{{transform:rotate(-135deg)}}
#fs2{{bottom:14px;right:14px;z-index:1001}}
#dl2{{top:14px;right:14px;z-index:1001}}
.caption{{position:absolute;left:0;right:0;bottom:0;display:none;padding:56px 24px 18px;color:#fff;text-align:center;font:500 15px/1.5 system-ui,sans-serif;text-shadow:0 1px 4px rgba(0,0,0,.75);white-space:pre-wrap;overflow:hidden;pointer-events:none;background:linear-gradient(to bottom,rgba(0,0,0,0) 0%,rgba(0,0,0,.3) 30%,rgba(0,0,0,.65) 65%,rgba(0,0,0,.88) 100%)}}
.caption.show{{display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical}}
</style>
</head>
<body>
<div id="c"{gallery_attr}{prompt_attr}>
<img src="{a}" draggable="false">
<img id="thumb" src="{b}" draggable="false">
<div id="d"></div>
<div id="h"><span></span><span></span></div>
<button id="fs" class="btn" title="Fullscreen" aria-label="Fullscreen">
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>
</button>
<button id="dl" class="btn" title="Download" aria-label="Download">
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
</button>
</div>
<div class="overlay" id="overlay">
<div id="c2">
<img src="{a}" draggable="false">
<img id="top2" src="{b}" draggable="false">
<div id="d2"></div>
<div id="h2"><span></span><span></span></div>
</div>
<div id="caption" class="caption"></div>
<button id="fs2" class="btn" title="Exit fullscreen" aria-label="Exit fullscreen">
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/></svg>
</button>
<button id="dl2" class="btn" title="Download" aria-label="Download">
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>
</button>
</div>
<script>
const c=document.getElementById('c'),topImg=document.getElementById('thumb'),
      im=document.querySelector('#c img'),
      c2=document.getElementById('c2'),top2=document.getElementById('top2'),
      im2=document.querySelector('#c2 img'),
      overlay=document.getElementById('overlay'),
      fsBtn=document.getElementById('fs'),fs2Btn=document.getElementById('fs2'),
      dlBtn=document.getElementById('dl'),dl2Btn=document.getElementById('dl2'),
      caption=document.getElementById('caption');
function reportHeight(){{parent.postMessage({{type:'iframe:height',height:document.documentElement.scrollHeight}},'*')}}
function isLandscape(){{
  if(screen.orientation&&screen.orientation.type)return screen.orientation.type.indexOf('landscape')===0;
  if(typeof window.orientation==='number')return Math.abs(window.orientation)===90;
  const sw=screen.width||0,sh=screen.height||0;
  return sw>sh&&sh>0;
}}
function fit(){{
  // Same sizing as the compare slider (§10): portrait full width no cap,
  // landscape capped at 80% of screen.availHeight. Skip while in browser
  // fullscreen (§10.8); re-fit on fullscreenchange.
  if(document.fullscreenElement||document.webkitFullscreenElement)return;
  if(!(im.naturalWidth>0&&im.naturalHeight>0)){{reportHeight();return;}}
  const r=im.naturalWidth/im.naturalHeight;
  const maxH=isLandscape()?(screen.availHeight||screen.height||0)*0.8:0;
  let w=document.documentElement.clientWidth;
  if(maxH>0){{const wByH=maxH*r;if(wByH>0&&wByH<w)w=wByH;}}
  c.style.width=w+'px';
  c.style.height=(w/r)+'px';
  reportHeight();
}}
function fitOverlay(){{
  // Size the overlay slider to the REAL viewport (in fullscreen the iframe
  // viewport IS the screen); wait for real dimensions (§10.4).
  if(!(im2.naturalWidth>0&&im2.naturalHeight>0))return;
  const r=im2.naturalWidth/im2.naturalHeight;
  const vw=document.documentElement.clientWidth||0,vh=document.documentElement.clientHeight||0;
  let w=vw,h=vw/r;
  if(h>vh){{h=vh;w=h*r;}}
  c2.style.width=w+'px';
  c2.style.height=h+'px';
}}
im.addEventListener('load',fit);
topImg.addEventListener('load',fit);
im2.addEventListener('load',fitOverlay);
top2.addEventListener('load',fitOverlay);
window.addEventListener('load',()=>{{fit();fitOverlay();}});
addEventListener('resize',fit);
addEventListener('resize',()=>{{if(document.fullscreenElement||document.webkitFullscreenElement)fitOverlay();}});
new ResizeObserver(fit).observe(document.body);
// Interactive slider — shared by the embed (#c) and the fullscreen overlay
// (#c2). Pointer Events unify mouse + touch (§10.5); touch-action:none keeps
// the browser from hijacking the gesture; the handle is purely visual
// (pointer-events:none). Ignore events on .btn (fullscreen button).
function setupSlider(el){{
  let dragging=false;
  function setP(x){{const rect=el.getBoundingClientRect(),p=Math.min(100,Math.max(0,(x-rect.left)/rect.width*100));el.style.setProperty('--p',p+'%');}}
  const onBtn=e=>e.target.closest&&e.target.closest('.btn');
  el.addEventListener('pointerdown',e=>{{if(onBtn(e))return;dragging=true;try{{el.setPointerCapture(e.pointerId)}}catch{{}}setP(e.clientX);e.preventDefault();}});
  el.addEventListener('pointermove',e=>{{if((dragging||e.pointerType==='mouse')&&!onBtn(e))setP(e.clientX);}});
  el.addEventListener('pointerup',()=>{{dragging=false;}});
  el.addEventListener('pointercancel',()=>{{dragging=false;}});
}}
setupSlider(c);
setupSlider(c2);
// Prompt caption (DESIGN.md §12): shown in the fullscreen only, as plain
// text over a bottom gradient (textContent, never innerHTML).
function showCaption(){{
  const p=c.getAttribute('data-prompt')||'';
  if(p){{caption.textContent=p;caption.classList.add('show');}}
  else{{caption.classList.remove('show');caption.textContent='';}}
}}
// Chat scroll preservation around the fullscreen (§10.8): the scroll is NOT
// on the parent window; it lives in an inner container in Open WebUI's DOM.
// Save the parent window AND all inner scrolled containers before opening,
// restore them after closing (same-origin ON; guarded for OFF).
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
function openFullscreen(){{
  overlay.classList.add('open');
  fitOverlay();
  showCaption();
  saveScroll();
  try{{overlay.requestFullscreen&&overlay.requestFullscreen();}}catch(e){{}}
  try{{overlay.webkitRequestFullscreen&&overlay.webkitRequestFullscreen();}}catch(e){{}}
}}
function closeFullscreen(){{
  if(document.fullscreenElement||document.webkitFullscreenElement){{
    try{{document.exitFullscreen&&document.exitFullscreen();}}catch(e){{}}
    try{{document.webkitExitFullscreen&&document.webkitExitFullscreen();}}catch(e){{}}
  }}else{{
    overlay.classList.remove('open');
    restoreScroll();
  }}
}}
fsBtn.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;openFullscreen();}});
fs2Btn.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;closeFullscreen();restoreScroll();}});
// Download (embed and fullscreen): fetch the result image as a blob and
// force a download; on failure (e.g. iOS sandboxed iframe) open it in a
// new tab as a fallback. The result image is the top layer of the slider
// (topImg in the embed, top2 in the overlay — same URL).
async function download(){{
  const src=topImg.src||top2.src||'';
  try{{const r=await fetch(src);if(!r.ok)throw new Error('HTTP '+r.status);const b=await r.blob();const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download='image.png';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),1000);}}catch(err){{const w=window.open(src,'_blank');if(w)w.focus();}}
}}
dlBtn.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;download();}});
dl2Btn.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;download();}});
overlay.addEventListener('pointerup',e=>{{if(e.target===overlay){{closeFullscreen();restoreScroll();}}}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape'){{closeFullscreen();restoreScroll();}}}});
document.addEventListener('fullscreenchange',()=>{{if(!(document.fullscreenElement||document.webkitFullscreenElement)){{overlay.classList.remove('open');restoreScroll();fit();}}}});
fit();
</script>
</body>
</html>
"""
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

        The result is displayed in the chat as a Rich UI embed: a
        before/after comparison slider (the same embed as compare_images /
        edit_image) showing the ORIGINAL model photo vs the try-on result,
        with the workflow-generated prompt as a caption in the fullscreen.
        The tool returns the image URL and the generated prompt as context
        ({'image': url, 'prompt': text}).

        :param model_image: Filename or URL of the person photo to dress.
            Required.
        :param upper_image: Filename or URL of the upper garment photo
            (top, jacket, shirt...). Optional.
        :param lower_image: Filename or URL of the lower garment photo
            (trousers, skirt, shorts...). Optional.
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
            # which the agent uses to reply to the user.
            # The result is a before/after comparison slider (the same embed
            # as compare_images/edit_image, DESIGN.md §10): the ORIGINAL model
            # photo vs the try-on result, in the chat embed AND in the
            # fullscreen overlay (floating maximize button, bottom-right). The
            # fullscreen shows only the comparison plus the prompt caption
            # (the workflow-generated prompt) and the exit button. The result
            # carries the image-viewer gallery markers (class=viewer +
            # data-gallery + id=thumb + data-prompt), so it is collectible by
            # the conversation gallery of the OTHER viewer embeds
            # (smart_generate_image), where it appears with its prompt. This
            # slider itself does NOT navigate the gallery (same constraint as
            # edit_image).
            # Both images share the same aspect ratio (the workflow keeps the
            # input size), so the slider's single-box sizing fits both with
            # object-fit:cover. The original URL is the passthrough argument
            # when model_image is a URL, or the temp-file URL (type=temp) when
            # it is a filename from a previous generation.
            parsed_model = urlparse(model_image)
            if parsed_model.scheme and parsed_model.netloc:
                original_url = model_image
            else:
                original_url = (
                    f"{base}/api/view?filename={model_image}&type=temp"
                )

            slider = self._build_compare_slider(
                original_url, image_url, gallery=True, prompt=prompt
            )
            return HTMLResponse(
                content=slider, headers={"Content-Disposition": "inline"}
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

"""
title: Edit Image
author: Insecure Erasure
description: Edit a previously generated image using Flux 2 inpainting/editing
version: 1.2
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
    ) -> str:
        """
        Build the before/after comparison slider as a standalone HTML document.

        This is the compare_images slider (DESIGN.md §10) extended for edit
        results: the embed shows the ORIGINAL image vs the EDITED one with an
        interactive divider, and the fullscreen overlay carries the same
        interactive slider PLUS the image-viewer features the lightbox has
        (DESIGN.md §11/§12): the prompt caption (only in fullscreen, over a
        bottom gradient), the download button, and the conversation gallery
        (prev/next/counter) when gallery=True.

        The two image URLs are injected into the <img> tags. They are
        HTML-escaped so query strings (e.g. &filename=...&type=...) cannot
        break the markup.

        Gallery: when gallery=True the container carries `class="viewer"`
        + `data-gallery="1"` and the edited <img> gets `id="thumb"` — the
        SAME markers the image viewer uses — so the gallery logic (in every
        viewer embed AND in this slider) collects the edited image with its
        prompt (data-prompt, HTML-escaped) exactly like any generated image.
        In the fullscreen overlay the gallery works in two modes: while on
        the embed's own image it shows the original-vs-edited slider; when
        navigating to another conversation image it shows that image alone
        (there is no original to compare it with). ArrowLeft/ArrowRight
        navigate, Escape/X/backdrop close, and the chat scroll is preserved
        around the fullscreen (saveScroll/restoreScroll, same-origin ON,
        guarded for OFF; DESIGN.md §10.8).

        Sizing: same as the compare_images slider — portrait full width no
        cap, landscape capped at 80% of the available screen height; both
        images share the aspect ratio (the edit keeps the input size).
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
body{{{{margin:0;background:#222}}}}
html,body{{{{height:100%;overflow:hidden;margin:0;padding:0}}}}
#c{{{{position:relative;width:100%;margin:0 auto;overflow:hidden;cursor:crosshair;touch-action:none;user-select:none;-webkit-user-select:none}}}}
#c img{{{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;-webkit-user-drag:none}}}}
.top{{{{clip-path:inset(0 calc(100% - var(--p,50%)) 0 0)}}}}
#d{{{{position:absolute;top:0;bottom:0;left:var(--p,50%);width:2px;background:rgba(255,255,255,.75);transform:translateX(-50%);pointer-events:none;mix-blend-mode:difference}}}}
#h{{{{position:absolute;top:50%;left:var(--p,50%);transform:translate(-50%,-50%);width:13px;height:18px;border-radius:4px;background:#fff;border:1px solid #333;box-shadow:0 1px 4px rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;gap:2px;cursor:grab;pointer-events:none}}}}
#h span{{{{width:3px;height:3px;border-left:1px solid #444;border-bottom:1px solid #444;transform:rotate(45deg)}}}}
#h span:last-child{{{{transform:rotate(-135deg)}}}}
.btn{{{{position:absolute;display:flex;align-items:center;justify-content:center;background:rgba(28,28,28,.75);border:none;border-radius:8px;color:#f5f5f5;cursor:pointer;padding:6px;z-index:5}}}}
.btn svg{{{{display:block}}}}
#fs{{{{bottom:8px;right:8px}}}}
@media (prefers-color-scheme: light){{{{
  .btn{{{{background:rgba(235,235,235,.82);color:#1a1a1a}}}}
}}}}
.overlay{{{{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:999}}}}
.overlay.open{{{{display:flex}}}}
#c2{{{{position:relative;overflow:hidden;cursor:crosshair;touch-action:none;user-select:none;-webkit-user-select:none;box-shadow:0 4px 30px rgba(0,0,0,.5);border-radius:4px}}}}
#c2 img{{{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;-webkit-user-drag:none}}}}
#top2{{{{clip-path:inset(0 calc(100% - var(--p,50%)) 0 0)}}}}
#d2{{{{position:absolute;top:0;bottom:0;left:var(--p,50%);width:2px;background:rgba(255,255,255,.75);transform:translateX(-50%);pointer-events:none;mix-blend-mode:difference}}}}
#h2{{{{position:absolute;top:50%;left:var(--p,50%);transform:translate(-50%,-50%);width:13px;height:18px;border-radius:4px;background:#fff;border:1px solid #333;box-shadow:0 1px 4px rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;gap:2px;cursor:grab;pointer-events:none}}}}
#h2 span{{{{width:3px;height:3px;border-left:1px solid #444;border-bottom:1px solid #444;transform:rotate(45deg)}}}}
#h2 span:last-child{{{{transform:rotate(-135deg)}}}}
#nav{{{{max-width:100vw;max-height:100vh;object-fit:contain;display:none;box-shadow:0 4px 30px rgba(0,0,0,.5);border-radius:4px}}}}
.btn.nav{{{{display:none}}}}
#close{{{{top:14px;left:14px}}}}
#dl{{{{top:14px;right:14px}}}}
#prev{{{{top:50%;left:14px;transform:translateY(-50%)}}}}
#next{{{{top:50%;right:14px;transform:translateY(-50%)}}}}
#fs2{{{{bottom:14px;right:14px;z-index:1001}}}}
.counter{{{{position:fixed;bottom:14px;right:14px;z-index:1000;display:none;align-items:center;justify-content:center;background:rgba(28,28,28,.75);color:#f5f5f5;border-radius:8px;padding:5px 12px;font:600 13px system-ui,sans-serif;pointer-events:none}}}}
.caption{{{{position:absolute;left:0;right:0;bottom:0;display:none;padding:56px 24px 18px;color:#fff;text-align:center;font:500 15px/1.5 system-ui,sans-serif;text-shadow:0 1px 4px rgba(0,0,0,.75);white-space:pre-wrap;overflow:hidden;pointer-events:none;background:linear-gradient(to bottom,rgba(0,0,0,0) 0%,rgba(0,0,0,.3) 30%,rgba(0,0,0,.65) 65%,rgba(0,0,0,.88) 100%)}}}}
.caption.show{{{{display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical}}}}
</style>
</head>
<body>
<div id="c"{gallery_attr}{prompt_attr}>
<img src="{a}" draggable="false">
<img id="thumb" class="top" src="{b}" draggable="false">
<div id="d"></div>
<div id="h"><span></span><span></span></div>
<button id="fs" class="btn" title="Fullscreen" aria-label="Fullscreen">
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3"/><path d="M21 8V5a2 2 0 0 0-2-2h-3"/><path d="M3 16v3a2 2 0 0 0 2 2h3"/><path d="M16 21h3a2 2 0 0 0 2-2v-3"/></svg>
</button>
</div>
<div class="overlay" id="overlay">
<div id="c2">
<img src="{a}" draggable="false">
<img id="top2" src="{b}" draggable="false">
<div id="d2"></div>
<div id="h2"><span></span><span></span></div>
</div>
<img id="nav" src="{b}" draggable="false">
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
<button id="fs2" class="btn" title="Exit fullscreen" aria-label="Exit fullscreen">
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/></svg>
</button>
<div id="counter" class="counter"></div>
</div>
<script>
const c=document.getElementById('c'),topImg=document.querySelector('.top'),
      im=document.querySelector('#c img'),
      c2=document.getElementById('c2'),top2=document.getElementById('top2'),
      im2=document.querySelector('#c2 img'),
      overlay=document.getElementById('overlay'),
      fsBtn=document.getElementById('fs'),fs2Btn=document.getElementById('fs2'),
      closeBtn=document.getElementById('close'),dlBtn=document.getElementById('dl'),
      prevBtn=document.getElementById('prev'),nextBtn=document.getElementById('next'),
      counter=document.getElementById('counter'),caption=document.getElementById('caption'),
      nav=document.getElementById('nav');
const ownSrc=document.getElementById('thumb').src,origSrc=document.querySelector('#c img').src;
const ownPrompt=c.getAttribute('data-prompt')||'';
function reportHeight(){{{{parent.postMessage({{{{type:'iframe:height',height:document.documentElement.scrollHeight}}}},'*')}}}}
function isLandscape(){{{{
  if(screen.orientation&&screen.orientation.type)return screen.orientation.type.indexOf('landscape')===0;
  if(typeof window.orientation==='number')return Math.abs(window.orientation)===90;
  const sw=screen.width||0,sh=screen.height||0;
  return sw>sh&&sh>0;
}}}}
function fit(){{{{
  // Same sizing as the compare slider (§10): portrait full width no cap,
  // landscape capped at 80% of screen.availHeight. Skip while in browser
  // fullscreen (§10.8); re-fit on fullscreenchange.
  if(document.fullscreenElement||document.webkitFullscreenElement)return;
  if(!(im.naturalWidth>0&&im.naturalHeight>0)){{{{reportHeight();return;}}}}
  const r=im.naturalWidth/im.naturalHeight;
  const maxH=isLandscape()?(screen.availHeight||screen.height||0)*0.8:0;
  let w=document.documentElement.clientWidth;
  if(maxH>0){{{{const wByH=maxH*r;if(wByH>0&&wByH<w)w=wByH;}}}}
  c.style.width=w+'px';
  c.style.height=(w/r)+'px';
  reportHeight();
}}}}
function fitOverlay(){{{{
  // Size the overlay slider to the REAL viewport (in fullscreen the iframe
  // viewport IS the screen); wait for real dimensions (§10.4).
  if(c2.style.display==='none')return;
  if(!(im2.naturalWidth>0&&im2.naturalHeight>0))return;
  const r=im2.naturalWidth/im2.naturalHeight;
  const vw=document.documentElement.clientWidth||0,vh=document.documentElement.clientHeight||0;
  let w=vw,h=vw/r;
  if(h>vh){{{{h=vh;w=h*r;}}}}
  c2.style.width=w+'px';
  c2.style.height=h+'px';
}}}}
function fitNav(){{{{
  // Size the single-image view (#nav) used when the gallery navigates away
  // from this embed's own image (no original to compare it with).
  if(nav.style.display==='none'||!(nav.naturalWidth>0&&nav.naturalHeight>0))return;
  const r=nav.naturalWidth/nav.naturalHeight;
  const vw=document.documentElement.clientWidth||0,vh=document.documentElement.clientHeight||0;
  let w=vw,h=vw/r;
  if(h>vh){{{{h=vh;w=h*r;}}}}
  nav.style.width=w+'px';
  nav.style.height=h+'px';
}}}}
im.addEventListener('load',fit);
topImg.addEventListener('load',fit);
im2.addEventListener('load',fitOverlay);
top2.addEventListener('load',fitOverlay);
nav.addEventListener('load',fitNav);
window.addEventListener('load',()=>{{{{fit();fitOverlay();fitNav();}}}});
addEventListener('resize',fit);
addEventListener('resize',()=>{{{{if(document.fullscreenElement||document.webkitFullscreenElement){{{{fitOverlay();fitNav();}}}}}}}});
new ResizeObserver(fit).observe(document.body);
// Interactive slider — shared by the embed (#c) and the fullscreen overlay
// (#c2). Pointer Events unify mouse + touch (§10.5); touch-action:none keeps
// the browser from hijacking the gesture; the handle is purely visual
// (pointer-events:none). Ignore events on .btn (fullscreen button).
function setupSlider(el){{{{
  let dragging=false;
  function setP(x){{{{const rect=el.getBoundingClientRect(),p=Math.min(100,Math.max(0,(x-rect.left)/rect.width*100));el.style.setProperty('--p',p+'%');}}}}
  const onBtn=e=>e.target.closest&&e.target.closest('.btn');
  el.addEventListener('pointerdown',e=>{{{{if(onBtn(e))return;dragging=true;try{{{{el.setPointerCapture(e.pointerId)}}}}catch{{{{}}}}setP(e.clientX);e.preventDefault();}}}});
  el.addEventListener('pointermove',e=>{{{{if((dragging||e.pointerType==='mouse')&&!onBtn(e))setP(e.clientX);}}}});
  el.addEventListener('pointerup',()=>{{{{dragging=false;}}}});
  el.addEventListener('pointercancel',()=>{{{{dragging=false;}}}});
}}}}
setupSlider(c);
setupSlider(c2);
// Gallery (DESIGN.md §11): collect every image in the chat whose embed
// carries the viewer data-gallery marker — this slider uses the SAME
// markers as the image viewer (class="viewer" + data-gallery on #c, the
// edited <img> as #thumb), so viewers AND sliders all appear together, each
// with its data-prompt caption. Requires same-origin ON; guarded for OFF.
let gallery=[],galleryIdx=-1;
function updateCaption(){{{{
  const p=(gallery[galleryIdx]&&gallery[galleryIdx].prompt)||'';
  if(p){{{{caption.textContent=p;caption.classList.add('show');}}}}
  else{{{{caption.classList.remove('show');caption.textContent='';}}}}
}}}}
function collectGallery(){{{{
  gallery=[];galleryIdx=-1;
  try{{{{
    const frames=parent.document.querySelectorAll('iframe');
    for(let i=0;i<frames.length;i++){{{{
      let cd=null;
      try{{{{cd=frames[i].contentDocument;}}}}catch(e){{{{}}}}
      if(!cd)continue;
      const v=cd.querySelector('.viewer[data-gallery]');
      if(!v)continue;
      const im=cd.getElementById('thumb');
      const pr=v.getAttribute('data-prompt')||'';
      if(im&&im.src&&!gallery.some(g=>g.src===im.src))gallery.push({{{{src:im.src,prompt:pr}}}});
    }}}}
  }}}}catch(e){{{{}}}}
  if(!gallery.some(g=>g.src===ownSrc))gallery.unshift({{{{src:ownSrc,prompt:ownPrompt}}}});
  galleryIdx=gallery.findIndex(g=>g.src===ownSrc);
  const multi=gallery.length>1;
  prevBtn.style.display=nextBtn.style.display=counter.style.display=multi?'flex':'none';
  if(multi&&galleryIdx>=0)counter.textContent=(galleryIdx+1)+'/'+gallery.length;
  updateCaption();
}}}}
function showImage(i){{{{
  if(gallery.length<2)return;
  galleryIdx=((i%gallery.length)+gallery.length)%gallery.length;
  const g=gallery[galleryIdx];
  counter.textContent=(galleryIdx+1)+'/'+gallery.length;
  if(g.src===ownSrc){{{{
    // Own image: back to the original-vs-edited comparison slider.
    nav.style.display='none';
    c2.style.display='block';
    im2.src=origSrc;top2.src=ownSrc;
    fitOverlay();
  }}}}else{{{{
    // Another conversation image: no original to compare, show it alone.
    c2.style.display='none';
    nav.style.display='block';
    nav.src=g.src;
    fitNav();
  }}}}
  updateCaption();
}}}}
// Chat scroll preservation around the fullscreen (§10.8): the scroll is NOT
// on the parent window; it lives in an inner container in Open WebUI's DOM.
// Save the parent window AND all inner scrolled containers before opening,
// restore them after closing (same-origin ON; guarded for OFF).
let savedScrolls=[];
function saveScroll(){{{{
  savedScrolls=[];
  try{{{{savedScrolls.push({{{{el:parent,top:parent.scrollY||0}}}});}}}}catch(e){{{{}}}}
  try{{{{
    const doc=parent.document||document;
    const all=doc.querySelectorAll&&doc.querySelectorAll('*');
    if(all)for(let i=0;i<all.length;i++){{{{
      const el=all[i];
      if(el.scrollTop>0&&el.scrollHeight>el.clientHeight)savedScrolls.push({{{{el:el,top:el.scrollTop}}}});
    }}}}
  }}}}catch(e){{{{}}}}
}}}}
function restoreScroll(){{{{requestAnimationFrame(()=>{{{{requestAnimationFrame(()=>{{{{
  try{{{{parent.scrollTo(0,savedScrolls[0]&&savedScrolls[0].top||0);}}}}catch(e){{{{}}}}
  for(let i=0;i<savedScrolls.length;i++){{{{try{{{{savedScrolls[i].el.scrollTop=savedScrolls[i].top;}}}}catch(e){{{{}}}}}}}}
  document.documentElement.scrollTop=0;document.body.scrollTop=0;
}}}});}}}});}}}}
function resetView(){{{{
  // Always reopen on the embed's own comparison (gallery may have navigated).
  nav.style.display='none';
  c2.style.display='block';
  im2.src=origSrc;top2.src=ownSrc;
}}}}
function openFullscreen(){{{{
  resetView();
  collectGallery();
  overlay.classList.add('open');
  fitOverlay();
  saveScroll();
  try{{{{overlay.requestFullscreen&&overlay.requestFullscreen();}}}}catch(e){{{{}}}}
  try{{{{overlay.webkitRequestFullscreen&&overlay.webkitRequestFullscreen();}}}}catch(e){{{{}}}}
}}}}
function closeLightbox(){{{{
  if(document.fullscreenElement||document.webkitFullscreenElement){{{{
    try{{{{document.exitFullscreen&&document.exitFullscreen();}}}}catch(e){{{{}}}}
    try{{{{document.webkitExitFullscreen&&document.webkitExitFullscreen();}}}}catch(e){{{{}}}}
  }}}}else{{{{
    overlay.classList.remove('open');
    restoreScroll();
  }}}}
}}}}
fsBtn.addEventListener('pointerup',e=>{{{{if(e.pointerType==='mouse'&&e.button!==0)return;openFullscreen();}}}});
fs2Btn.addEventListener('pointerup',e=>{{{{if(e.pointerType==='mouse'&&e.button!==0)return;closeLightbox();restoreScroll();}}}});
closeBtn.addEventListener('pointerup',()=>{{{{closeLightbox();restoreScroll();}}}});
overlay.addEventListener('pointerup',e=>{{{{if(e.target===overlay){{{{closeLightbox();restoreScroll();}}}}}}}});
prevBtn.addEventListener('pointerup',e=>{{{{e.stopPropagation();showImage(galleryIdx-1);}}}});
nextBtn.addEventListener('pointerup',e=>{{{{e.stopPropagation();showImage(galleryIdx+1);}}}});
document.addEventListener('keydown',e=>{{{{
  if(e.key==='Escape'){{{{closeLightbox();restoreScroll();}}}}
  else if(e.key==='ArrowLeft'){{{{if(overlay.classList.contains('open'))showImage(galleryIdx-1);}}}}
  else if(e.key==='ArrowRight'){{{{if(overlay.classList.contains('open'))showImage(galleryIdx+1);}}}}
}}}});
document.addEventListener('fullscreenchange',()=>{{{{if(!(document.fullscreenElement||document.webkitFullscreenElement)){{{{overlay.classList.remove('open');restoreScroll();fit();}}}}}}}});
async function download(){{{{try{{{{const r=await fetch((gallery[galleryIdx]&&gallery[galleryIdx].src)||ownSrc);if(!r.ok)throw new Error('HTTP '+r.status);const b=await r.blob();const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download='image.png';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),1000);}}}}catch(err){{{{const w=window.open((gallery[galleryIdx]&&gallery[galleryIdx].src)||ownSrc,'_blank');if(w)w.focus();}}}}}}}}
dlBtn.addEventListener('pointerup',download);
fit();
</script>
</body>
</html>
"""
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

        The edited image is displayed in the chat as a Rich UI embed (a
        before/after comparison slider, original vs edited, the same embed as
        compare_images). The tool returns the image URL as context
        ({'image': <url>}); use it for chained tool calls (upscale_image,
        virtual_try_on, generate_video) or to refer to the edited image.

        :param image: The filename previously generated from
            smart_generate_image or upscale_image, or a direct URL to an
            external image.
        :param edit_prompt: Natural language description of the edit to apply
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
            _, edit_node = _resolve_node(workflow, "Prompt")
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
            preview_image_id, _ = _resolve_node(workflow, "Random Preview Image")

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
                            "description": "\u2705 Image edited.",
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
                original_url, edit_url, gallery=True, prompt=edit_prompt
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

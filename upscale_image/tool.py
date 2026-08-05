"""
title: Upscale Image
author: Insecure Erasure
description: Upscale an image by its name or URL
version: 1.2
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
    """Poll /history/{prompt_id} until the workflow completes."""
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
            description=(
                "Public base URL for image links (overrides "
                "COMFYUI_BASE_URL). Leave empty to use COMFYUI_BASE_URL."
            ),
        )

    class UserValves(BaseModel):
        """User-level configuration (overrides admin valve)."""

        comfyui_image_base_url: str = Field(
            default="",
            description=(
                "Public base URL for image links. "
                "Overrides the admin valve and COMFYUI_BASE_URL."
            ),
        )

    def __init__(self):
        self.valves = self.Valves()
        self.citation = False


    def _build_compare_slider(self, image_a: str, image_b: str) -> str:
        """
        Build the before/after comparison slider as a standalone HTML document
        (the same embed as compare_images, DESIGN.md §10).

        The two image URLs are injected into the <img> tags. They are
        HTML-escaped so query strings (e.g. &filename=...&type=...) cannot
        break the markup.

        The slider fills the full width of the chat container and its height
        follows the aspect ratio of the base image (image_a), with an adaptive
        sizing strategy (portrait: full width, no cap; landscape: height capped
        at 80% of the available vertical space, width scaled proportionally and
        centered). The original and the upscaled image share the same aspect
        ratio (SeedVR2 preserves it), so a single box fits both with
        object-fit:cover.

        A fullscreen mode is available via a floating button at the bottom-right
        (standard maximize icon): it opens the comparison in a fullscreen
        overlay with its OWN interactive slider (same drag/tap/hover/divider
        behavior). The overlay is fullscreened via the browser Fullscreen API
        on the overlay element (not documentElement, DESIGN.md §10.8) so the
        chat scroll is preserved (saveScroll/restoreScroll, same-origin ON,
        guarded for OFF); the embed's fit() skips sizing while in fullscreen
        and re-fits on fullscreenchange (§10.8).
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
html,body{{height:100%;overflow:hidden;margin:0;padding:0}}
#c{{position:relative;width:100%;margin:0 auto;overflow:hidden;cursor:crosshair;touch-action:none;user-select:none;-webkit-user-select:none}}
#c img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block;-webkit-user-drag:none}}
#top{{clip-path:inset(0 calc(100% - var(--p,50%)) 0 0)}}
#d{{position:absolute;top:0;bottom:0;left:var(--p,50%);width:2px;background:rgba(255,255,255,.75);transform:translateX(-50%);pointer-events:none;mix-blend-mode:difference}}
#h{{position:absolute;top:50%;left:var(--p,50%);transform:translate(-50%,-50%);width:13px;height:18px;border-radius:4px;background:#fff;border:1px solid #333;box-shadow:0 1px 4px rgba(0,0,0,.3);display:flex;align-items:center;justify-content:center;gap:2px;cursor:grab;pointer-events:none}}
#h span{{width:3px;height:3px;border-left:1px solid #444;border-bottom:1px solid #444;transform:rotate(45deg)}}
#h span:last-child{{transform:rotate(-135deg)}}
.btn{{position:absolute;display:flex;align-items:center;justify-content:center;background:rgba(28,28,28,.75);border:none;border-radius:8px;color:#f5f5f5;cursor:pointer;padding:6px;z-index:5}}
.btn svg{{display:block}}
#fs{{bottom:8px;right:8px}}
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
</style>
</head>
<body>
<div id="c">
<img src="{a}" draggable="false">
<img id="top" src="{b}" draggable="false">
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
<button id="fs2" class="btn" title="Exit fullscreen" aria-label="Exit fullscreen">
  <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3v3a2 2 0 0 1-2 2H3"/><path d="M21 8h-3a2 2 0 0 1-2-2V3"/><path d="M3 16h3a2 2 0 0 1 2 2v3"/><path d="M16 21v-3a2 2 0 0 1 2-2h3"/></svg>
</button>
</div>
<script>
const c=document.getElementById('c'),topImg=document.getElementById('top'),
      im=document.querySelector('#c img'),
      c2=document.getElementById('c2'),top2=document.getElementById('top2'),
      im2=document.querySelector('#c2 img'),
      overlay=document.getElementById('overlay'),
      fsBtn=document.getElementById('fs'),fs2Btn=document.getElementById('fs2');
function reportHeight(){{parent.postMessage({{type:'iframe:height',height:c.offsetHeight||document.documentElement.scrollHeight}},'*')}}
function isLandscape(){{
  if(screen.orientation&&screen.orientation.type)return screen.orientation.type.indexOf('landscape')===0;
  if(typeof window.orientation==='number')return Math.abs(window.orientation)===90;
  const sw=screen.width||0,sh=screen.height||0;
  return sw>sh&&sh>0;
}}
function fit(){{
  // Only size the slider once the base image has real dimensions (DESIGN.md
  // §10.4). Skip sizing while in browser fullscreen: the iframe viewport is
  // then the full screen, and resizing/reporting would blow up the embed and
  // shift the chat scroll (§10.8) — the overlay covers everything anyway;
  // re-fit on fullscreenchange when leaving.
  if(document.fullscreenElement||document.webkitFullscreenElement)return;
  if(!(im.naturalWidth>0&&im.naturalHeight>0)){{reportHeight();return;}}
  const r=im.naturalWidth/im.naturalHeight;
  // Adaptive sizing (unchanged): portrait full width no cap; landscape cap
  // 80% of the available vertical space, width scaled proportionally.
  // Orientation detection is conservative (degenerate/ambiguous → portrait).
  const maxH=isLandscape()?(screen.availHeight||screen.height||0)*0.8:0;
  let w=document.documentElement.clientWidth;
  if(maxH>0){{const wByH=maxH*r;if(wByH>0&&wByH<w)w=wByH;}}
  c.style.width=w+'px';
  c.style.height=(w/r)+'px';
  reportHeight();
}}
function fitOverlay(){{
  // The overlay slider is sized to the REAL viewport: in fullscreen the
  // iframe viewport IS the screen, so no orientation heuristic is needed —
  // contain the images (same aspect ratio assumed) within it, centered.
  // Wait for real dimensions (§10.4); the URLs are the same cached ones as
  // the embed, and the load listeners below cover the cold case.
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
// (#c2). Pointer Events unify mouse + touch: on desktop the divider follows
// the mouse on hover (no click), a click/tap jumps, dragging works with any
// pointer; touch-action:none keeps the browser from hijacking the gesture;
// the handle is a purely visual affordance (pointer-events:none). The
// fullscreen button (#fs) lives inside #c, so the handlers ignore any event
// whose target is a .btn to keep it from moving the divider.
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
// Fullscreen (DESIGN.md §10.7/§10.8): fullscreen the OVERLAY element (not
// documentElement) so the parent chat does not scroll to top on enter; the
// overlay slider is interactive exactly like the embed one. Escape, the
// restore (minimize) button, or clicking the dark backdrop exit. The chat
// scroll lives in an inner container in Open WebUI's DOM — save/restore it
// around the fullscreen (same-origin ON; guarded with try/catch so it also
// works OFF).
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
overlay.addEventListener('pointerup',e=>{{if(e.target===overlay){{closeFullscreen();restoreScroll();}}}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape'){{closeFullscreen();restoreScroll();}}}});
document.addEventListener('fullscreenchange',()=>{{
  if(!(document.fullscreenElement||document.webkitFullscreenElement)){{
    overlay.classList.remove('open');
    restoreScroll();
    fit();
  }}else{{
    fitOverlay();
  }}
}});
// If the base image was already loaded (e.g. from cache) before this script
// ran, fit() uses its real dimensions immediately; otherwise it reports the
// current height and the load events correct it when the image arrives.
fit();
</script>
</body>
</html>
"""

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

        :param image: The filename previously generated from the
            smart_generate_image response, or a direct URL to an external image.
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
            raw_workflow = _load_workflow(__id__, "seedvr2_upscale.json")
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
            upscaled_filename, image_type = _extract_image_filename(
                outputs, preview_image_id
            )

            base = resolved_image_base_url.rstrip("/")
            upscaled_url = (
                f"{base}/api/view?filename={upscaled_filename}&type={image_type}"
            )

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
            # The result is a before/after comparison slider (the same embed
            # as compare_images, DESIGN.md §10): the ORIGINAL image vs the
            # UPSCALED one, in the chat embed AND in the fullscreen overlay
            # (floating maximize button, bottom-right). Both images share the
            # same aspect ratio (SeedVR2 preserves it), so the slider's
            # single-box sizing fits both with object-fit:cover. The original
            # URL is the passthrough argument when it is a URL, or the
            # temp-file URL (type=temp — the same directory the Load Image
            # node reads from) when it is a filename from a previous
            # generation.
            if parsed.scheme and parsed.netloc:
                original_url = image
            else:
                original_url = (
                    f"{base}/api/view?filename={image}&type=temp"
                )

            slider = self._build_compare_slider(original_url, upscaled_url)
            return HTMLResponse(
                content=slider, headers={"Content-Disposition": "inline"}
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
                "Do not retry. Acknowledge the cancellation and wait "
                "for their next request."
            )
        except Exception as e:
            log.exception("upscale failed: %s", e)
            return f"Error upscaling image: {e}"

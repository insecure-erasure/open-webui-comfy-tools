"""
Shared embed builders for the Rich UI migration (see DESIGN.md).

Central source of truth for the image-viewer embed HTML used by the image
tools (smart_generate_image, edit_image, enhance_image, virtual_try_on).

Lesson learned in compare_images (DESIGN.md §10): the sandboxed iframe starts
at ~150px and cannot read the parent viewport, so the viewer reports its own
height via postMessage, approximates viewport-relative caps with the device
screen, waits for real image dimensions before sizing, and uses Pointer
Events for interaction.
"""

import html


def build_image_viewer(image_url: str, aspect_ratio: tuple[int, int] | None = None) -> str:
    """
    Build the self-contained image viewer embed for a single image URL.

    The URL is HTML-escaped so query strings (e.g. &filename=...&type=...)
    cannot break the markup.

    Layout: the image is centered and fits the chat container width, with a
    height cap of 70vh. If aspect_ratio (reduced_w, reduced_h) is provided,
    the embed reserves that aspect before the image loads to avoid the
    "jump"; otherwise it sizes after the image has real dimensions.

    Clicking the image opens a lightbox overlay: the image is fit to the
    screen (no scroll), an X in the top-left closes it, and a download button
    in the top-right forces a download (fetch blob -> object URL -> anchor
    with download attr). The theme follows prefers-color-scheme.
    """
    src = html.escape(image_url, quote=True)
    if aspect_ratio:
        w, h = aspect_ratio
        if w > 0 and h > 0:
            reserved = f"aspect-ratio:{w}/{h};"
        else:
            reserved = ""
    else:
        reserved = ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{
  color-scheme:light dark;
}}
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%}}
body{{display:flex;align-items:center;justify-content:center;background:transparent}}
img{{-webkit-user-drag:none;user-select:none;-webkit-user-select:none}}
.viewer{{max-width:100%;{reserved}max-height:70vh;display:flex;align-items:center;justify-content:center;overflow:hidden;cursor:zoom-in;border-radius:12px}}
.viewer img{{display:block;max-width:100%;max-height:70vh;object-fit:contain;border-radius:12px}}
.overlay{{position:fixed;inset:0;background:rgba(0,0,0,.82);display:none;align-items:center;justify-content:center;z-index:999}}
.overlay.open{{display:flex}}
.overlay img{{max-width:100vw;max-height:100vh;object-fit:contain;box-shadow:0 4px 30px rgba(0,0,0,.5);border-radius:4px}}
.btn{{position:fixed;z-index:1000;display:flex;align-items:center;justify-content:center;background:rgba(28,28,28,.75);border:none;border-radius:8px;color:#f5f5f5;cursor:pointer;padding:6px}}
.btn svg{{display:block}}
#close{{top:14px;left:14px}}
#dl{{top:14px;right:14px}}
@media (prefers-color-scheme: light){{
  .btn{{background:rgba(235,235,235,.82);color:#1a1a1a}}
}}
</style>
</head>
<body>
<div class="viewer" id="viewer">
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
</div>
<script>
const viewer=document.getElementById('viewer'),thumb=document.getElementById('thumb'),
      overlay=document.getElementById('overlay'),big=document.getElementById('big'),
      closeBtn=document.getElementById('close'),dlBtn=document.getElementById('dl');
function reportHeight(){{parent.postMessage({{type:'iframe:height',height:document.documentElement.scrollHeight}},'*')}}
function fit(){{reportHeight()}}
// Resize the iframe once the image has real dimensions (see DESIGN.md §10.4).
thumb.addEventListener('load',fit);
big.addEventListener('load',fit);
window.addEventListener('load',fit);
addEventListener('resize',fit);
new ResizeObserver(fit).observe(document.body);
// Report the initial height so the iframe is not stuck at ~150px.
fit();
function openLightbox(){{overlay.classList.add('open');document.body.style.overflow='hidden'}}
function closeLightbox(){{overlay.classList.remove('open');document.body.style.overflow=''}}
viewer.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;if(e.detail===0||e.pointerType!=='mouse'){{openLightbox();return;}}openLightbox();}});
closeBtn.addEventListener('pointerup',closeLightbox);
overlay.addEventListener('pointerup',e=>{{if(e.target===overlay)closeLightbox();}});
big.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;if(e.detail===0||e.pointerType!=='mouse'){{return;}}e.stopPropagation();}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape')closeLightbox();}});
async function download(){{try{{const r=await fetch(big.src);if(!r.ok)throw new Error('HTTP '+r.status);const b=await r.blob();const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download='image.png';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),1000);}}catch(err){{const w=window.open(big.src,'_blank');if(w)w.focus();}}}}
dlBtn.addEventListener('pointerup',download);
</script>
</body>
</html>
"""

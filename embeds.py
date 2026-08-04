"""
Shared embed builders for the Rich UI migration (see DESIGN.md).

Central source of truth for the image-viewer embed HTML used by the image
tools (smart_generate_image, edit_image, enhance_image, virtual_try_on).

IMPORTANT: Open WebUI runs each tool as a single self-contained module (the
script pasted in Workspace → Tools) and cannot import repo modules like this
one. Therefore each tool embeds its own copy of the viewer as a local method
(`_build_image_viewer`), and this module is the reference that the tool copies
are generated from. Keep the copies byte-identical to this source to avoid
drift (see DESIGN.md Appendix B).

Lesson learned in compare_images (DESIGN.md §10): the sandboxed iframe starts
at ~150px and cannot read the parent viewport, so `vh`/`vw` units inside the
embed are useless (they refer to the iframe box, not the browser). The viewer
sizes itself from the container width + image aspect ratio, caps the height
at 70% of the available screen height (screen.availHeight), and reports its
height via postMessage. The lightbox uses the browser Fullscreen API (the
Open WebUI iframe has allowfullscreen) so the image fills the browser window
instead of the embed box; it falls back to the embed area where fullscreen is
not available (e.g. some mobile browsers).
"""

import html


def build_image_viewer(image_url: str, aspect_ratio: tuple[int, int] | None = None) -> str:
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
    """
    src = html.escape(image_url, quote=True)
    if aspect_ratio:
        w, h = aspect_ratio
        if w > 0 and h > 0:
            ratio_js = f"{w}/{h}"
        else:
            ratio_js = "null"
    else:
        ratio_js = "null"

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
.viewer{{max-width:100%;overflow:hidden;cursor:zoom-in;border-radius:12px}}
.viewer img{{display:block;width:100%;height:100%;object-fit:contain;border-radius:12px}}
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
const RESERVED_R={ratio_js};
function reportHeight(){{parent.postMessage({{type:'iframe:height',height:viewer.offsetHeight||document.documentElement.scrollHeight}},'*')}}
function log(msg){{try{{console.log('[viewer]',msg,'| overlay=',overlay.className,'| fs=',!!(document.fullscreenElement||document.webkitFullscreenElement),'| viewerH=',viewer.offsetHeight,'| docH=',document.documentElement.scrollHeight);}}catch(e){{}}}}
function fit(){{log('fit() enter');
  // Sizing replicates the compare_images slider (DESIGN.md §10): the iframe
  // starts at ~150px and its own vh is useless, so derive from the container
  // width and the image aspect ratio, cap the height at 70% of the available
  // screen height (screen.availHeight), and report the resulting height.
  // While the lightbox is in browser fullscreen the iframe viewport is the
  // full screen and resizing/reporting would blow up the embed and shift the
  // chat scroll — so skip sizing during fullscreen entirely (the overlay
  // covers everything anyway) and re-fit when fullscreen ends.
  if(document.fullscreenElement||document.webkitFullscreenElement){{log('fit() skip (fullscreen)');return;}}
  let r=0;
  if(RESERVED_R)r=Number(RESERVED_R);
  if(!(r>0)&&thumb.naturalWidth>0&&thumb.naturalHeight>0)r=thumb.naturalWidth/thumb.naturalHeight;
  if(!(r>0)){{log('fit() no ratio yet');reportHeight();return;}}
  const maxH=(screen.availHeight||screen.height||0)*0.7;
  let w=document.documentElement.clientWidth;
  if(maxH>0){{const wByH=maxH*r;if(wByH>0&&wByH<w)w=wByH;}}
  viewer.style.width=w+'px';
  viewer.style.height=(w/r)+'px';
  reportHeight();
  log('fit() sized w='+w+' h='+(w/r));
}}
thumb.addEventListener('load',fit);
big.addEventListener('load',fit);
window.addEventListener('load',fit);
addEventListener('resize',fit);
new ResizeObserver(fit).observe(document.body);
fit();
function openLightbox(){{
  overlay.classList.add('open');
  document.body.style.overflow='hidden';
  log('openLightbox: request fullscreen');
  // Fill the browser window, not just the embed box: request fullscreen on
  // the document. The Open WebUI iframe has allowfullscreen; inside the
  // sandbox this requires a user gesture (the click that just happened).
  // Browsers that reject it (e.g. some mobile) fall back to the embed area.
  const el=document.documentElement;
  try{{el.requestFullscreen&&el.requestFullscreen();}}catch(e){{log('requestFullscreen err '+e);}}
  try{{el.webkitRequestFullscreen&&el.webkitRequestFullscreen();}}catch(e){{log('webkitRequestFullscreen err '+e);}}
}}
function closeLightbox(){{
  log('closeLightbox: fs='+(!!(document.fullscreenElement||document.webkitFullscreenElement)));
  // If in fullscreen, exit it; the overlay is removed and the size re-fit in
  // the fullscreenchange handler (avoids a flash of the bare viewer fullscreen).
  if(document.fullscreenElement||document.webkitFullscreenElement){{
    try{{document.exitFullscreen&&document.exitFullscreen();}}catch(e){{log('exitFullscreen err '+e);}}
    try{{document.webkitExitFullscreen&&document.webkitExitFullscreen();}}catch(e){{log('webkitExitFullscreen err '+e);}}
  }}else{{
    overlay.classList.remove('open');
    document.body.style.overflow='';
    restoreScroll();
  }}
}}
// Restore the parent scroll position after fullscreen: exiting fullscreen
// scrolls the iframe's document back to the top, and since the iframe height
// changes when the overlay opens, the chat scroll jumps up. Save the parent
// scroll position before opening and restore it after closing. NOTE: in the
// cross-origin sandbox parent.scrollTo is blocked, so the real fix is not
// resizing the embed during fullscreen (see fit()); this is a best-effort
// fallback for the embed-area lightbox (no fullscreen).
let savedScroll=0;
function saveScroll(){{try{{savedScroll=(parent.scrollY||0)||(document.documentElement.scrollTop||0);}}catch(e){{savedScroll=0;}}}}
function restoreScroll(){{requestAnimationFrame(()=>{{try{{parent.scrollTo(0,savedScroll);}}catch(e){{}}document.documentElement.scrollTop=savedScroll;document.body.scrollTop=savedScroll;}});}}
viewer.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;saveScroll();openLightbox();}});
closeBtn.addEventListener('pointerup',()=>{{closeLightbox();restoreScroll();}});
overlay.addEventListener('pointerup',e=>{{if(e.target===overlay){{closeLightbox();restoreScroll();}}}});
big.addEventListener('pointerup',e=>{{if(e.pointerType==='mouse'&&e.button!==0)return;e.stopPropagation();}});
document.addEventListener('keydown',e=>{{if(e.key==='Escape'){{closeLightbox();restoreScroll();}}}});
document.addEventListener('fullscreenchange',()=>{{log('fullscreenchange fs='+(!!(document.fullscreenElement||document.webkitFullscreenElement)));if(!(document.fullscreenElement||document.webkitFullscreenElement)){{overlay.classList.remove('open');document.body.style.overflow='';restoreScroll();fit();}}}});
async function download(){{try{{const r=await fetch(big.src);if(!r.ok)throw new Error('HTTP '+r.status);const b=await r.blob();const u=URL.createObjectURL(b);const a=document.createElement('a');a.href=u;a.download='image.png';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),1000);}}catch(err){{const w=window.open(big.src,'_blank');if(w)w.focus();}}}}
dlBtn.addEventListener('pointerup',download);
</script>
</body>
</html>
"""

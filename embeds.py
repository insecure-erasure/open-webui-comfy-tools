"""
Shared embed builders for the Rich UI migration (see DESIGN.md).

Central source of truth for the image-viewer embed HTML used by the image
tools (smart_generate_image, virtual_try_on).

Notes (2026-08-05): enhance_image was renamed to upscale_image and migrated
from the image viewer to the before/after comparison slider (original vs
upscaled, the compare_images embed). edit_image followed the same migration
(original vs edited). Neither consumes the viewer anymore — their reference
embed lives in compare_images/tool.py (_build_slider_html).

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

Gallery (cross-cutting feature, 2026-08-04): the viewer carries a
`data-gallery` marker (only when built with gallery=True). The marker is the
ONLY thing the tool contributes — there is NO backend/Python gallery logic in
the tools (maintainer constraint). All gallery logic is JS inside the embed:
when a gallery lightbox opens it walks the parent chat DOM (same-origin ON;
guarded, so same-origin OFF just yields an empty gallery) and collects every
image in the conversation whose viewer carries the marker, offering prev/next
buttons (‹ ›, vertically centered), a "n/N" counter (bottom-right) and
ArrowLeft/ArrowRight keyboard navigation with wrap-around. See DESIGN.md §11.
"""

import html


def build_image_viewer(image_url: str, aspect_ratio: tuple[int, int] | None = None, gallery: bool = False, prompt: str | None = None) -> str:
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
    attribute. Opening the lightbox then walks the parent chat DOM (same-origin
    ON — guarded, so same-origin OFF just yields an empty gallery) and collects
    every image in the conversation whose viewer carries the marker. The
    lightbox shows ‹ › buttons (vertically centered), a "n/N" counter
    (bottom-right), and ArrowLeft/ArrowRight keyboard navigation with
    wrap-around. The download button keeps using `big.src`, so it always
    downloads the image currently shown. The gallery is ephemeral: it is
    rebuilt each time the lightbox opens, and navigating never modifies the
    chat or the thumbnails.

    Prompt caption: when prompt is provided it is added as a `data-prompt`
    attribute (HTML-escaped) — another HTML identifier, never backend logic.
    In the lightbox only (never the thumbnail), a gradient overlay at the
    bottom shows the prompt in white: the gradient goes from transparent at
    the top to dark at the bottom so the white text (in the darkest zone)
    stays readable over any image content; a subtle text-shadow reinforces it.
    When the gallery navigates, the caption follows the shown image's prompt.
    A failed image load is retried once (no watchdog): on `error` the img src
    is cleared and re-set a single time per URL (flaky/slow fetches), after
    which a second failure is left alone (the browser shows the alt text).
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


def build_video_player(video_url: str) -> str:
    """
    Build the self-contained video player embed for a single video URL.

    The URL is HTML-escaped so query strings (e.g. &filename=...&type=...)
    cannot break the markup.

    Layout: the player fits the chat container width and its height is capped
    at 65% of the available screen height (screen.availHeight) — the sizing
    decision recorded in DESIGN.md §6 (2026-08-04): 65vh. `vh` units inside
    the sandboxed iframe are useless (they refer to the iframe box, ~150px),
    so the cap is expressed via the device screen, exactly like the image
    viewer. The video's aspect ratio is NOT known a priori (unlike
    smart_generate_image, which reserves reduced_w:reduced_h), so the embed
    waits for the `loadedmetadata` event (videoWidth/videoHeight) before
    sizing — never a made-up fallback ratio (DESIGN.md §10.4) — and reports
    the player's own height via reportHeight() so the iframe hugs the video
    (no empty frame on wide desktop screens).

    The player uses the native controls (play/seek/volume/fullscreen); there
    is no lightbox and no download button (maintainer decision, 2026-08-04).
    `muted` is kept so autoplay works (browsers block autoplay with sound).
    The native fullscreen of the <video> (via its controls) does NOT cause
    the chat scroll jump, so no saveScroll/restoreScroll is needed here
    (unlike the image lightbox, DESIGN.md §10.8).
    """
    src = html.escape(video_url, quote=True)

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
html,body{{height:100%;overflow:hidden;margin:0;padding:0}}
body{{display:flex;align-items:center;justify-content:center;background:transparent}}
.player{{max-width:100%;overflow:hidden;border-radius:12px;background:#000}}
.player video{{display:block;width:100%;height:100%;object-fit:contain;border-radius:12px}}
</style>
</head>
<body>
<div class="player" id="player">
  <video id="video" src="{src}" autoplay muted loop playsinline controls preload="metadata"></video>
</div>
<script>
const player=document.getElementById('player'),video=document.getElementById('video');
function reportHeight(){{parent.postMessage({{type:'iframe:height',height:player.offsetHeight||document.documentElement.scrollHeight}},'*')}}
function fit(){{
  // The video's aspect ratio is not known a priori (unlike
  // smart_generate_image, which reserves reduced_w:reduced_h): wait for the
  // real dimensions before sizing — never fall back to a made-up ratio
  // (DESIGN.md §10.4). Until then, report the current height and let the
  // media events correct it.
  if(!(video.videoWidth>0&&video.videoHeight>0)){{reportHeight();return;}}
  const r=video.videoWidth/video.videoHeight;
  // Sizing decision (DESIGN.md §6, 2026-08-04): 65vh cap. vh/vw units are
  // useless inside the sandboxed iframe (§10.7), so the cap is 65% of the
  // available screen height (screen.availHeight); the width derives from
  // the container width + aspect ratio and the height never overflows the
  // available screen space.
  const maxH=(screen.availHeight||screen.height||0)*0.65;
  let w=document.documentElement.clientWidth;
  if(maxH>0){{const wByH=maxH*r;if(wByH>0&&wByH<w)w=wByH;}}
  player.style.width=w+'px';
  player.style.height=(w/r)+'px';
  reportHeight();
}}
video.addEventListener('loadedmetadata',fit);
video.addEventListener('loadeddata',fit);
video.addEventListener('canplay',fit);
window.addEventListener('load',fit);
addEventListener('resize',fit);
new ResizeObserver(fit).observe(document.body);
fit();
</script>
</body>
</html>
"""

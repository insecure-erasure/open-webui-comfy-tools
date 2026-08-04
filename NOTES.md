# NOTES — Handover for the next session

Status and handover notes for the Rich UI migration (branch `refactor/native_embeds`).
Conversation language with the maintainer: **Spanish**. Code and docs: **English**.

---

## Where we are

The Rich UI migration (see `DESIGN.md` and `PLAN.md`) is **complete** for all
six tools. The video phase
(`generate_video`) was **approved by the maintainer** (2026-08-04, 65vh cap,
no download). The **image gallery** (Phase 8, post-migration) is
**implemented and awaiting maintainer test** (controls visible only with >1
image in the conversation). Only the cross-cutting
cleanup (Phase 7) remains.

| Phase | Tool | Status |
|---|---|---|
| 0 | Documentation (`DESIGN.md`, `PLAN.md`) | ✅ done |
| 1 | `compare_images` | ✅ approved |
| 2 | `smart_generate_image` | ✅ approved |
| 3 | `edit_image` | ✅ approved |
| 4 | `enhance_image` | ✅ approved |
| 5 | `virtual_try_on` | ✅ approved |
| 6 | `generate_video` | ✅ **approved by maintainer** (65vh cap, no download; decision in DESIGN.md §6) |
| 7 | Cross-cutting cleanup | ⏳ mostly pending — duplicated `reportHeight` in image viewers **deduped** (2026-08-04); grep `image_md`, main README, CORS notes left |

## How to resume (next session)

1. **Read `DESIGN.md` first** — especially §6 (the **65vh / no-download decision** + the "Implementation notes for the video embed" block) and §10 (lessons learned).
2. **Phase 6 — `generate_video`**: **APPROVED by maintainer (2026-08-04)** — the video player embed (reference: `embeds.build_video_player`; tool copy: `_build_video_player`) sizes after `loadedmetadata` with a 65% `screen.availHeight` cap and native controls (no lightbox/download). The 65vh cap was chosen because the sandboxed iframe cannot deduct the Open WebUI input bar from `screen.availHeight`, so a more aggressive cap leaves room for vertical videos to fit without clipping.
3. **Phase 7 — cleanup**: grep the repo for leftover `image_md`, `image_filename`, "Wrap the HTML block" instructions (now only in docs); update the main `README.md` tools section (done for video; verify the rest); verify CORS notes in §7 against the real reverse-proxy config (user-owned). ~~Dedupe the duplicated `reportHeight()` in the image viewer (embeds.py + 4 image-tool copies)~~ — **DONE (2026-08-04, commit below)**: one line removed per file, all copies verified byte-identical.
4. Follow the working agreement in `PLAN.md`: **one phase at a time**; after
   each phase's commit, return control to the maintainer for testing; next
   phase only after explicit approval.

## Environment facts (important)

- The maintainer's Open WebUI runs with **Iframe Sandbox Allow Same Origin ON**
  (Settings → Interface). This differs from the `DESIGN.md` default (OFF).
  Consequence: the embed shares origin with the parent, so `parent.document`
  is accessible and the chat scroll can be restored by walking the parent's
  scrolled containers (the fix for the scroll jump, §10.8). The viewer code
  guards parent access in try/catch so it also works with same-origin OFF.
- CORS decision: **allow-all** (`Access-Control-Allow-Origin: *`) on the
  ComfyUI proxy; same-origin proxying was considered and parked (§7).
- Images/videos hang from an internal TLD (`akari.private`-like); Open WebUI
  serves the embeds; both HTTP (no mixed content).

## Architecture reminders (so the next agent doesn't re-learn them)

- **Each tool is a self-contained module** pasted into Open WebUI
  (Workspace → Tools). It **cannot import repo modules** (e.g. `embeds`). The
  image-viewer HTML is embedded as a local `_build_image_viewer` method inside
  each tool, **byte-identical** to `embeds.py` (the reference). Keep copies
  identical to avoid drift. `embeds.py` is a repo helper only — the reference
  that tool copies are generated from; it is NOT importable at runtime.
- Tools return `(HTMLResponse, context)` with `Content-Disposition: inline`.
  The middleware emits the `embeds` event; the frontend renders a sandboxed
  iframe; the LLM only receives the `context`.
  - Image tools: `context = {"image": <url>}`.
  - `virtual_try_on`: `context = {"image": <url>, "prompt": <text>}` (the
    prompt is the only justified exception).
  - Terminal tools (`compare_images`, `generate_video`): **bare `HTMLResponse`**
    (no tuple) → the LLM gets the middleware's generic message. An empty dict
    `{}` IS sent to the LLM as context — do not use it.
- **Prompt caption in the lightbox (2026-08-04)**: in fullscreen only, images
  generated from a prompt show it in white at the bottom over a gradient
  (transparent top → dark bottom, `.88` under the text + `text-shadow`). Only
  tools that accept a prompt: SGI (`prompt`), edit (`edit_prompt`), try-on
  (workflow prompt); **enhance has NO prompt → no caption**. `data-prompt`
  HTML-escaped on `.viewer` (HTML identifier only, no backend logic); gallery
  now collects `{src,prompt}` and the caption follows navigation; `textContent`
  never `innerHTML`. 18 node cases; f-strings byte-identical (13310 chars).
- **Failed-load retry (2026-08-04, cheap fix, no watchdog)**: occasionally a
  slow/flaky fetch left the embed without the image (user had to "reload this
  frame"). The symptom is a failed fetch, NOT a layout issue — a watchdog that
  re-calls `fit()` cannot fix a missing download (and is hacky). Implemented
  the standard cheap fix: on `img error`, clear + re-set `src` once per URL
  (`retryOnce` in the viewer f-string). Verify with 8 node cases; gallery's 16
  still pass.
- **Gallery scope limitation (2026-08-04)**: the gallery only sees the images
  of **mounted** messages — Open WebUI renders only the last ~8 messages
  (`Messages.svelte` `messagesCount=8`; older messages are unmounted from the
  DOM until the user scrolls up, which reloads the previous window). Not data
  loss: it mirrors what the chat itself shows. Also fixed a real bug:
  `collectGallery` now reads `#thumb.src` (stable identity) instead of
  `#big.src` (mutated by gallery navigation → images dropped/duplicated after
  navigating), and `openLightbox()` resets `big.src=thumb.src`. 16 node cases.
- **The image gallery (Phase 8, 2026-08-04, awaiting test)**: from any image
  lightbox (fullscreen), ‹ › buttons walk the images generated in the chat
  starting from the one being viewed. **Maintainer constraint: NO
  backend/Python gallery logic in the tools** — the tool only adds an HTML
  identifier (`data-gallery="1"` on `.viewer`, via `gallery=True`); all
  gallery logic is JS inside the embed. `collectGallery()` walks the parent
  chat DOM (same-origin ON; guarded → degrades to a single image, controls
  hidden). Controls: ‹ › vertically centered, "n/N" counter **bottom-right**,
  ArrowLeft/ArrowRight while the overlay is open, **wrap-around**; the
  download button keeps using `big.src`. The 4 image tools pass
  `gallery=True`; the 4 f-strings stay byte-identical to `embeds.py` (10560
  chars). The NOTES.md regeneration script below now emits the `gallery`
  parameter + docstring. 13 node cases pass.
- **The viewer sizing** (image): width = container width, height capped at 70% of `screen.availHeight`, `reportHeight()` = `viewer.offsetHeight` (iframe hugs the image). `smart_generate_image` reserves the aspect ratio (`reduced_w:reduced_h`); the other image tools don't (they size after load).
- **The video embed (Phase 6)**: terminal result → **bare `HTMLResponse`** (no tuple), like `compare_images`. The player is a `<video autoplay muted loop playsinline controls>` sized from `videoWidth/videoHeight` after `loadedmetadata` (ratio NOT known a priori — no aspect reservation), capped at **65% of `screen.availHeight`** (65vh decision), `reportHeight()` = player's own height. **No lightbox and no download button** (decision): native controls give fullscreen, and native video fullscreen does NOT cause the chat scroll jump, so there is no saveScroll/restoreScroll. The iframe still must never scroll (`overflow:hidden`). Reference builder: `embeds.build_video_player` (no duplicated reportHeight there — the duplication only exists in the image viewer).
- **Lightbox**: fullscreen the `.overlay` element (not `documentElement`),
  skip sizing while in fullscreen, restore the chat scroll on close by walking
  the parent's scrolled containers (`saveScroll`/`restoreScroll`, double rAF).
- **compare_images**: interactive before/after slider with adaptive sizing (portrait full width / landscape 80%% cap), pointer events, hover on desktop. **Fullscreen (2026-08-04, approved)**: floating bottom-right button (maximize icon) opens a fullscreen overlay with its OWN interactive slider (embed behavior unchanged); Fullscreen API on the overlay element (+ webkit fallback), sized to the real viewport waiting for real dimensions; exit via Escape / restore button (bottom-right, icon flips inward) / backdrop; saveScroll/restoreScroll + fit() skipped while fullscreen, re-fit on fullscreenchange (same §10.8 pattern as the image lightbox).

## How to regenerate a tool copy from `embeds.py`

```bash
# from repo root, with the venv python (which has importlib/inspect):
venv/bin/python - <<'PYEOF'
import importlib.util, inspect, re, textwrap
spec = importlib.util.spec_from_file_location('embeds', 'embeds.py')
emb = importlib.util.module_from_spec(spec); spec.loader.exec_module(emb)
src = inspect.getsource(emb.build_image_viewer)
lines = src.splitlines(); body = lines[1:]
ret_idx = next(i for i,l in enumerate(body) if l.strip().startswith('return f"""'))
pre_lines, fstr_lines = body[:ret_idx], body[ret_idx:]
pre = textwrap.dedent('\n'.join(pre_lines))
pre_indented = '\n'.join(('        ' + l) if l.strip() else '' for l in pre.splitlines())
fstring = re.search(r'return (f""".*?""")', '\n'.join(fstr_lines), re.S).group(1)
method = ('    def _build_image_viewer(self, image_url: str, aspect_ratio: tuple[int, int] | None = None, gallery: bool = False, prompt: str | None = None) -> str:\n'
          + pre_indented + '\n'
          '        return (\n' + fstring + '\n        )\n')
# then insert `method` after the tool's __init__ (see the tool files), and
# pass gallery=True (all four image tools) and prompt=... from the return
# path ONLY for tools with a prompt input (SGI, edit, try-on; NOT enhance).
# The method docstring must also be kept in sync (gallery + prompt caption
# paragraphs, §11-12).
PYEOF
```

Verify with: load both modules (stub fastapi/httpx/pydantic), call
`Tools()._build_image_viewer(url, aspect_ratio=...)` in the tool and
`embeds.build_image_viewer(url, aspect_ratio=...)` and assert equal.

## Gotchas that cost a lot of time (do not repeat)

- When regenerating f-strings from `embeds.py` with a regex, the image viewer
  f-string closes with THREE newlines before `def build_video_player` — match
  with `"""\n\s*def build_video_player` (a literal `\n\n\n` broke the first
  attempt).
- **`context` must be a plain dict** `{"image": url}` — double braces
  `{{"image": url}}` (from an f-string) become a **set containing a dict** →
  `unhashable type: 'dict'`. Watch for this whenever editing returns that came
  from `image_md` f-strings.
- **`vh`/`vw` inside the sandboxed iframe** refer to the iframe box (~150px),
  NOT the browser viewport. Use `screen.availHeight` for viewport-relative
  caps.
- **The chat scroll is NOT on the parent window** (`parent.scrollY` stays 0);
  it lives in an inner overflow container in Open WebUI's DOM. Restore it by
  walking `parent.document.querySelectorAll('*')` for scrolled elements.
- **Firefox warning** "An iframe which has both allow-scripts and
  allow-same-origin for its sandbox attribute can remove its sandboxing" is a
  **real signal** that the iframe has same-origin (the user's Open WebUI has it
  ON) — not a false positive.
- Console logs from a `srcdoc` iframe appear under the **iframe's document
  context** in DevTools — select the iframe frame to see them.
- When debugging sizing/scroll: instrument with `[viewer]` console logs and
  reproduce — don't guess (§10.8).

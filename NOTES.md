# NOTES — Handover for the next session

Status and handover notes for the Rich UI migration (branch `refactor/native_embeds`).
Conversation language with the maintainer: **Spanish**. Code and docs: **English**.

---

## Where we are

The Rich UI migration (see `DESIGN.md` and `PLAN.md`) is **almost complete**. All
image tools are migrated and **approved by the maintainer**. Only the video phase
remains, postponed to a future session.

| Phase | Tool | Status |
|---|---|---|
| 0 | Documentation (`DESIGN.md`, `PLAN.md`) | ✅ done |
| 1 | `compare_images` | ✅ approved |
| 2 | `smart_generate_image` | ✅ approved |
| 3 | `edit_image` | ✅ approved |
| 4 | `enhance_image` | ✅ approved |
| 5 | `virtual_try_on` | ✅ approved |
| 6 | `generate_video` | ⏸️ **postponed** — sizing decision A/B/C pending |
| 7 | Cross-cutting cleanup | ⏳ pending (grep `image_md`, main README, CORS notes) |

## How to resume (next session)

1. **Read `DESIGN.md` first** — especially §6 (video sizing decision + the
   "Implementation notes for the video embed" block) and §10 (lessons learned).
   The video phase must not repeat the mistakes of Phases 1-5.
2. **Phase 6 — `generate_video`**: before implementing, the maintainer must
   decide the video embed sizing option (§6: **A** fill available area / **B**
   70vh like images / **C** max width + proportional height). The decision is
   recorded in §6 when made.
3. **Phase 7 — cleanup**: grep the repo for leftover `image_md`,
   `image_filename`, "Wrap the HTML block" instructions; update the main
   `README.md` tools section; verify CORS notes in §7 against the real
   reverse-proxy config (user-owned).
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
- **The viewer sizing** (image): width = container width, height capped at
  70% of `screen.availHeight`, `reportHeight()` = `viewer.offsetHeight`
  (iframe hugs the image). `smart_generate_image` reserves the aspect ratio
  (`reduced_w:reduced_h`); the other image tools don't (they size after load).
- **Lightbox**: fullscreen the `.overlay` element (not `documentElement`),
  skip sizing while in fullscreen, restore the chat scroll on close by walking
  the parent's scrolled containers (`saveScroll`/`restoreScroll`, double rAF).
- **compare_images** is a plain slider (no lightbox/fullscreen): adaptive
  sizing (portrait full width / landscape 80% cap), pointer events, hover on
  desktop.

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
method = ('    def _build_image_viewer(self, image_url: str, aspect_ratio: tuple[int, int] | None = None) -> str:\n'
          + pre_indented + '\n'
          '        return (\n' + fstring + '\n        )\n')
# then insert `method` after the tool's __init__ (see the tool files)
PYEOF
```

Verify with: load both modules (stub fastapi/httpx/pydantic), call
`Tools()._build_image_viewer(url, aspect_ratio=...)` in the tool and
`embeds.build_image_viewer(url, aspect_ratio=...)` and assert equal.

## Gotchas that cost a lot of time (do not repeat)

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

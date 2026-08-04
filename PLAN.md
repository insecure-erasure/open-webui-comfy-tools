# Implementation Plan — Rich UI Migration (open-webui-comfy-tools)

Execution order and progress tracking for the migration described in [DESIGN.md](DESIGN.md).
Language of code and docs: **English**. Conversation/notes with the maintainer: Spanish.

**Status legend**: `[ ]` not started · `[~]` in progress · `[x]` done · `[?]` blocked / needs decision

## Working agreement

- **One phase at a time.** After each phase's implementation is committed, **control returns to the maintainer** for testing the result of that phase.
- The next phase only starts after the maintainer gives the go-ahead (explicit approval or "continue").
- If a test fails or the maintainer requests changes, the phase is reopened (status back to `[~]`) until approved.
- No two phases are implemented back to back without an explicit green light in between.

---

## Phase 0 — Documentation (this branch)

- [x] `DESIGN.md` written (design notes + source verification against open-webui `main`).
- [x] `PLAN.md` created (this file).
- [ ] `compare_images/README.md` already documents Rich UI — verify it matches the final code.

**Notes**
- Branch: `refactor/native_embeds` (clean, identical to `master` at start).
- Scope: `compare_images`, `smart_generate_image`, `edit_image`, `enhance_image`, `virtual_try_on`, `generate_video`. `generate_caption` is **out of scope** (returns text only).

---

## Phase 1 — `compare_images` (foundation / pattern validation)

First because it is the simplest tool (no ComfyUI, no workflow) and its README already describes the Rich UI version — the code is migrated to match the README, validating the whole pattern end to end.

- [ ] Return `HTMLResponse` with `Content-Disposition: inline` instead of the markdown-hack string.
- [ ] **Empty context**: return the bare `HTMLResponse` (no tuple) so the LLM receives the middleware's generic message. Do **not** return `{}` (an empty dict *is* sent to the LLM as context — see DESIGN.md §A.1).
- [ ] Keep the slider HTML (escaped URLs, divider 50%) and ensure it follows the `reportHeight()` contract (load + image load + resize + ResizeObserver).
- [ ] Keep `urlparse` validation of both URLs and the error/status messages.
- [ ] Update README if anything drifted.

**Definition of done**: comparing two URLs renders the slider as a sandboxed embed; the LLM does not see any HTML; no instruction to the agent.

**Notes**
- This phase defines the shared patterns (embed + height reporting) that the image/video tools reuse.

---

## Phase 2 — `smart_generate_image` (image viewer pattern)

- [ ] Build the **shared image-viewer embed** HTML: 70vh cap, centered flexbox, `object-fit: contain`, aspect reservation using `reduced_w:reduced_h` (known a priori), lightbox (click → full-screen overlay, zoom = fit to screen, X close top-left, forced-download button top-right, dark/semi-transparent background, `prefers-color-scheme`).
- [ ] Return `(HTMLResponse, context)` with `context = {"image": <url>}` (the **URL**, not the filename).
- [ ] Remove `image_md:` / `image_filename:` from the returned string; remove the instruction to the agent.
- [ ] Update the tool docstring so the agent knows the URL is actionable for downstream tools (edit/enhance/virtual_try_on/video).
- [ ] Update README.

**Notes**
- This tool is the reference implementation for §5 of DESIGN.md (general rule for image-returning tools).
- The viewer HTML written here is the copy-paste source for Phases 3–5.

---

## Phase 3 — `edit_image`

- [ ] Same pattern as `smart_generate_image`: `(HTMLResponse, context)`, `context = {"image": <url>}`.
- [ ] Viewer identical to Phase 2's, but **no aspect reservation** (output dimensions unknown a priori → rely on `reportHeight()` after image load; see DESIGN.md Appendix B).
- [ ] Remove `image_md:` / `image_filename:` and the agent instruction.
- [ ] Update docstring + README.

---

## Phase 4 — `enhance_image`

- [ ] Same pattern as Phase 3: `(HTMLResponse, context)`, `context = {"image": <url>}`, viewer without aspect reservation.
- [ ] Remove `image_md:` / `image_filename:` and the agent instruction.
- [ ] Update docstring + README.

---

## Phase 5 — `virtual_try_on`

- [ ] Same pattern, with the only justified context exception: `context = {"image": <url>, "prompt": <text>}` (prompt from `_extract_text`, used by the agent to reply to the user).
- [ ] Viewer identical to the image viewer (no aspect reservation).
- [ ] Remove `image_md:` / `image_filename:` and the agent instruction.
- [ ] Update docstring + README.

---

## Phase 6 — `generate_video` (terminal, pending decision)

- [ ] **BLOCKED / PENDING DECISION** — video embed sizing, DESIGN.md §6: choose **A** (fill available area, no restrictive cap) / **B** (70vh cap like images) / **C** (max width + proportional height). Decision is recorded in DESIGN.md §6 when made.
- [ ] Return `HTMLResponse` with a `<video autoplay muted loop playsinline>` element (keep `muted` so autoplay works).
- [ ] **Empty context**: bare `HTMLResponse` (generic middleware message).
- [ ] `reportHeight()` contract for the video container.
- [ ] Remove the fragile HTML-block reproduction hack (the current one is the most at risk of losing attributes / breaking).
- [ ] Update docstring + README.

---

## Phase 7 — Cross-cutting cleanup

- [ ] Grep the repo for leftover `image_md`, `image_filename`, "Wrap the HTML block" instructions.
- [ ] Main `README.md` tools section: update descriptions that mention `image_md` / markdown rendering.
- [ ] Verify CORS notes in DESIGN.md §7 against the actual reverse-proxy config (user-owned, outside the repo).

---

## Progress log

| Date | Phase | What was done | Notes |
|------|-------|---------------|-------|
| 2026-08-04 | 0 | DESIGN.md and PLAN.md written | Verified middleware/iface against open-webui `main`; video sizing decision deferred (§6) |
| 2026-08-04 | 0 | Scope confirmed with maintainer | `smart_fetch_url` = `smart_generate_image`; docs/code in English; `generate_caption` out of scope |
| 2026-08-04 | 0 | Working agreement added to PLAN.md | One phase at a time; control returns to maintainer after each phase for testing; next phase only after go-ahead |
| 2026-08-04 | 1 | `compare_images` migrated to Rich UI embed | `(HTMLResponse)` bare with `Content-Disposition: inline`, empty context (generic middleware message), divider 50%, `reportHeight()` contract, README aligned |
| 2026-08-04 | 1 | `compare_images` sizing fix (phase reopened) | Adaptive strategy: portrait = full width no cap; landscape = 80%% of available height cap (approximated via `screen.availHeight`), width scaled + centered. Tested in node (4 cases). Commit `c6c20e9` |
| 2026-08-04 | 1 | Mobile bug: cap fired on portrait (phase reopened) | Conservative orientation detection: `screen.orientation` → `window.orientation` → width/height; degenerate/ambiguous → portrait (no cap). 8 node cases pass incl. 0×0 webview. CORS decision: accept allow-all (`*`), same-origin proxy parked. Commit `…` |

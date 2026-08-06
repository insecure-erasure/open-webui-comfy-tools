# Refactor: extract embed HTML out of the tool code

Status: **viable** — proof of concept implemented on `generate_video` (this branch).

Goal: surgically remove the HTML f-string blobs from each tool's Python and
store them in a standalone HTML file with the same name as the tool
(`<tool>/<tool>.html`, mirroring the existing `<tool>/<tool>.json` workflow
convention). The Python builders become thin wrappers: escape values, load
the template from the cache dir, inject placeholders.

Out of scope for now: `generate_caption` (returns plain text, no embed).
`embeds.py` was removed after the migration (its content now lives in the
`.html` templates, which are the single source of truth).

---

## 1. Inventory of the blobs (all measured on master, ad6e1db)

| Tool | Builder | Blob lines | Placeholders |
|---|---|---|---|
| `compare_images/tool.py` | `_build_slider_html(a, b)` (module fn) | 66–254 (189) | `{a}` ×2, `{b}` ×2 |
| `smart_generate_image/tool.py` | `_build_image_viewer(url, ratio, gallery, prompt)` | 442–672 (231) | `{src}` ×2, `{gallery_attr}`, `{prompt_attr}`, `{ratio_js}` |
| `edit_image/tool.py` | `_build_compare_slider(a, b, gallery, prompt)` | 300–475 (176) | `{a}` ×2, `{b}` ×2, `{gallery_attr}`, `{prompt_attr}` |
| `upscale_image/tool.py` | `_build_compare_slider(a, b)` | 243–431 (189) | `{a}` ×2, `{b}` ×2 |
| `virtual_try_on/tool.py` | `_build_compare_slider(a, b, gallery, prompt)` | 341–535 (195) | `{a}` ×2, `{b}` ×2, `{gallery_attr}`, `{prompt_attr}` |
| `generate_video/tool.py` | `_build_video_player(url)` | 612–665 (54) | `{src}` |

~1,034 lines of HTML would leave the Python. The embed-behavior docstrings
(the ones that documented the markup, not the code) move into **HTML header
comments** in the `.html` files, so the knowledge stays next to the code it
describes; the `.py` builders keep a short reference instead.

Each `.html` starts with a `<!-- -->` header comment containing the original
`_build_*` docstring (behavior documentation), then the markup with the
injection tokens. The builders only compute the escaped values and inject.

## 2. Why it is viable

**The runtime precedent already exists.** Every image/video tool already reads
a file from `CACHE_DIR / 'tools' / <tool_id> / <filename>` on **every
invocation** — the workflow JSON (`_load_workflow(__id__, ...)`). Open WebUI
injects `__id__` (the tool's DB id, which is also the cache subdir name, created
automatically when the tool is saved) and creates the cache directory. Reading
`<tool>.html` from that same directory is mechanically identical: a `_load_embed`
twin of `_load_workflow`, with the same helpful `FileNotFoundError` that tells
the user to copy the file.

**The output is unchanged end-to-end.** The tool still returns the same HTML
string in the same `HTMLResponse(content=..., headers={"Content-Disposition":
"inline"})`; Open WebUI's middleware never sees a difference. Proven for the
PoC: the old f-string render and the new template render are identical
(modulo a trailing newline) for a normal URL and for a URL full of
HTML-special characters (`&`, `<`, `"`) — escaping stays in Python.

**Placeholder injection is collision-free.** The blobs contain zero `$` and the
`{token}` names occur exactly as counted above (no accidental `{a}`/`{b}`/`{src}`
inside CSS/JS — verified by counting). Keep the f-string tokens in the HTML file
(`{a}`, `{b}`, `{src}`, `{gallery_attr}`, `{prompt_attr}`, `{ratio_js}`) and
inject with ordered `str.replace()`. This makes each `.html` file a **verbatim
un-escaped copy of the old f-string body** (`{{`→`{`, `}}`→`}`), which keeps
review diffs tiny and preserves the current Python-side escaping logic.

**Per-tool cost is negligible.** One small file read per call, exactly like the
workflow JSON already read per call. No caching needed to stay consistent.

## 3. Design of the new pattern (as implemented in the PoC)

```python
# module level, right after _load_workflow
def _load_embed(tool_id: str, filename: str) -> str:
    """CACHE_DIR / 'tools' / <tool_id> / <filename>; RuntimeError if no tool_id,
    FileNotFoundError with a copy-this-file hint if missing."""
    ...

class Tools:
    def _build_video_player(self, video_url: str, tool_id: str = "") -> str:
        src = html.escape(video_url, quote=True)
        template = _load_embed(tool_id, "generate_video.html")
        return template.replace("{src}", src)

    # call site (has __id__ in scope):
    player = self._build_video_player(video_url, tool_id=__id__)
```

- Builders keep their signatures modulo a trailing `tool_id: str = ""`; default
  empty reproduces `_load_workflow`'s clear "must run inside Open WebUI" error
  for direct-call tests.
- `compare_images` needs `__id__: str = ""` added to `compare_images()` — the
  only tool that does not yet declare it.
- `smart_generate_image` computes `ratio_js`, `gallery_attr`, `prompt_attr`
  exactly as today, then injects.

## 4. Verification strategy (per tool)

1. Render old (git HEAD f-string) vs new (template + replace) with a normal URL
   and a special-chars URL → assert equal (modulo trailing newline).
2. `python3 -m py_compile <tool>/tool.py`.
3. Diff review: the `.html` file must equal the old f-string body with
   `{{`→`{`, `}}`→`}` only.

## 5. Deployment / docs impact

- README step 2 (Deploy the workflow JSONs) must also copy each `<tool>.html`:
  `cp <tool>/<tool>.html /app/backend/data/cache/tools/<tool>/<tool>.html`.
- The loader's error message tells the user the exact copy command when the
  file is missing.

## 6. Risks and open decisions

- **`embeds.py` removed / drift invariant (DESIGN.md Appendix B, NOTES.md)**: the
  image-viewer markup in `smart_generate_image.html` is no longer kept
  byte-identical to a Python reference — the `.html` templates are the source
  of truth. Keep the shared markup/JS in sync across the slider/viewer
  variants when the shared behavior changes (they differ legitimately: plain /
  marker-bearing / marker+caption). Flagged, resolved by removal.
- **Slider duplication remains**: `compare_images`, `upscale_image`,
  `edit_image`, `virtual_try_on` each keep their own slider `.html` (they are
  genuinely different variants — plain / marker-bearing / marker+caption).
  The refactor moves that duplication from un-editable f-strings into real
  HTML files, which is the point; it does not deduplicate.
- **`_build_*` tests that call builders directly** (NOTES.md pattern) now need
  `tool_id` or a template argument; only `smart_generate_image` had a documented
  cross-check (vs `embeds.py`), which is now moot — verify against the
  `.html` template instead.
- **Pre-existing README staleness** (tool renamed `enhance_image` →
  `upscale_image`, deploy table still lists `enhance_image`) is out of scope;
  the new HTML lines should use the real directory names.
- If a template is missing at runtime the tool errors **after** the ComfyUI
  work is done; worth keeping the error message crisp (done) — same exposure the
  workflow JSON already has.

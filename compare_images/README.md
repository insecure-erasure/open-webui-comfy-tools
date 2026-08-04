# Compare Images

Compares two images side by side with an interactive before/after slider, rendered directly in the chat. Unlike the other tools in this repo, it does not talk to ComfyUI and requires no workflow: it just receives two image URLs and returns an HTML snippet.

## Usage

The LLM calls this tool when the user asks to compare two images (e.g. before/after editing or enhancement, or two generated variants). It takes two arguments:

| Argument | Description |
|---|---|
| image_a | URL of the first image. Base layer, fully visible; appears on the right of the divider. |
| image_b | URL of the second image. Top layer, revealed by the slider from the left of the divider. |

Both arguments should be the image URLs produced by the other tools (e.g. `http://open-webui.private/api/v1/files/<id>/content`). The tool validates that both look like URLs and returns a helpful error otherwise.

## How it renders

The tool returns a bare `HTMLResponse` with `Content-Disposition: inline` (no context tuple), so Open WebUI renders it as a **Rich UI embed**: an interactive sandboxed iframe right in the chat (see the official [Rich UI Embedding](https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/) docs). The tool also includes the recommended `iframe:height` postMessage so the iframe auto-sizes to the slider.

Because the comparison is a **terminal result** (it does not chain with any other tool), the LLM receives only the middleware's generic message ("Embedded UI result is active and visible to the user.") and no tool-specific context — the HTML never enters the LLM context.

The slider uses the skeleton from the request with three adjustments:

- The image URLs are HTML-escaped so query strings (e.g. `&filename=...&type=...`) cannot break the `src` attributes.
- The divider starts at 50% instead of 0% so both images are visible on load. Change the two `var(--p,50%)` fallbacks back to `0%` if you prefer the original behavior.
- A `reportHeight()` postMessage keeps the embed height in sync with the slider size (recommended by Open WebUI for sandboxed embeds).

The interaction uses **Pointer Events**, which unify mouse and touch. On desktop, the divider **follows the mouse while hovering** over the slider (no click needed); a **click/tap** jumps it to that position; and **dragging** works with any pointer (mouse, touch, pen). A small narrow **handle** with arrows is shown in the center of the divider as a visual affordance that the slider can be moved. `touch-action: none` on the container keeps the browser from hijacking the drag for scrolling.

The handle is **small (~40% of the original size, 13x18px) and fully opaque** (solid background). The divider itself uses `mix-blend-mode: difference` with a semi-transparent white, so it appears **translucent and inverts the colors** of the image it passes over.

### Robust image-load handling

The slider area is only sized once the base image has real dimensions. The previous fallback ratio (16:9) mis-sized the area when the images were not loaded yet, and if the image was already cached the `load` event never fired to correct it (a frame reload fixed it visually). Now `fit()` waits for `naturalWidth`/`naturalHeight`, and re-runs on the image `load` events, the window `load`, `resize`, and a `ResizeObserver`. Both images are assumed to share the same aspect ratio; the area follows the base image (`image_a`).

### Fullscreen mode

A floating **fullscreen button** (bottom-right corner of the slider, the standard maximize icon) opens the comparison in a **fullscreen overlay** with its own interactive slider — the same drag/tap/hover/divider behavior, so the comparison stays interactive at full size. It uses the browser **Fullscreen API** (the Open WebUI iframe has `allowfullscreen`, the same mechanism the image viewer's lightbox uses), so it fills the browser window; on browsers that reject it (e.g. some mobile/iOS) the overlay falls back to the embed area. Exit with **Escape**, the **restore (minimize) button** (bottom-right, the icon flips to an inward-pointing frame), or clicking the dark backdrop. The chat scroll is preserved around the fullscreen (the embed's `fit()` is skipped while in fullscreen and re-runs on exit, so the chat position does not jump).

## Sizing strategy (adaptive)

The slider uses an adaptive sizing strategy depending on the device orientation:

- **Vertical (portrait) devices**: the slider fills the full width of the chat container with **no height cap** (height follows the image aspect ratio).
- **Horizontal (landscape) devices**: the height is **capped at 80% of the available vertical space**; the width is scaled proportionally and the slider is centered.

Because the embed is a sandboxed cross-origin iframe, it cannot read the parent page's viewport. Device orientation is therefore detected inside the embed: `screen.orientation` when available, then `window.orientation`, then a `screen.width`/`screen.height` comparison. Detection is **conservative**: degenerate screen values (e.g. 0-height in some webviews) or any ambiguity are treated as portrait, so the height cap can never fire on a portrait device.

## Requirements

- A recent Open WebUI version with Rich UI embedding support (renders the tool's `HTMLResponse` as an interactive iframe).
- No ComfyUI server, no workflow file, no custom nodes.

## Installation

Add the tool in Workspace > Tools (suggested name: **Compare Images**). There is no workflow JSON to deploy.

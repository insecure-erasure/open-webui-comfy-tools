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

The interaction is mouse-driven (`mousemove` on the container); touch is not handled.

## Requirements

- A recent Open WebUI version with Rich UI embedding support (renders the tool's `HTMLResponse` as an interactive iframe).
- No ComfyUI server, no workflow file, no custom nodes.

## Installation

Add the tool in Workspace > Tools (suggested name: **Compare Images**). There is no workflow JSON to deploy.

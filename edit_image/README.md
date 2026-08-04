# Edit Image

Edits a previously generated image using Flux 2 inpainting. Accepts either a tool-generated filename or an external URL. The image source is auto-detected: if the input has a scheme and netloc it is treated as a URL, otherwise as a temporary file in ComfyUI's output directory.

## Valves

### Admin

| Valve | Description |
|---|---|
| steps | Inference steps. 0 = use workflow default (6). |
| lora_config | JSON array of LoRAs applied positionally. |
| comfyui_image_base_url | Override the image link base URL. |

### User

| Valve | Description |
|---|---|
| steps | Inference steps. 0 = use workflow default. |
| lora_config | JSON array. Merged with admin LoRAs; user wins on name collision. |
| override_system_loras | When enabled, user LoRAs replace admin LoRAs entirely. |
| comfyui_image_base_url | Overrides admin valve and COMFYUI_BASE_URL. |

## Usage

The LLM calls this tool when the user explicitly asks to edit or modify an existing image. The edit_prompt describes the desired change in natural language (e.g., "change the background to a beach at sunset"). A random seed is generated for each edit.

## How it renders

The tool returns an `HTMLResponse` with `Content-Disposition: inline` plus a context tuple, so Open WebUI renders it as a **Rich UI embed**: a self-contained image viewer in a sandboxed iframe right in the chat (same viewer as Smart Generate Image).

- The image is centered, fits the chat container width, and its height is capped at **70% of the available screen height**; the viewer sizes itself after the image loads (the output dimensions are not known in advance, so there is no aspect reservation).
- Clicking the image opens a **lightbox** that fills the browser window via the **Fullscreen API** (image fit to screen, no scroll), with an X to close (top-left) and a **download** button (top-right) that forces the download.
- On close, the chat scroll position is preserved (no jump to the top).

The **LLM only receives the context** `{ "image": <url> }` (the edited image URL) — never the HTML. The URL is the actionable value for chained tool calls.

## Workflow file

Place `edit_image.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/edit_image/edit_image.json
```

The workflow JSON can be edited freely. You can replace the default Flux 2 model with any compatible checkpoint. The tool injects parameters from both the LLM call arguments (image, edit_prompt) and the valves. Everything else uses whatever the workflow defines.

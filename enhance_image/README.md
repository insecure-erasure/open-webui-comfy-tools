# Enhance Image

Upscales or enhances a previously generated image using SeedVR2. Loads images via URL or temporary file, auto-detecting the source type. Requires the ComfyUI-LoadImageURL custom node.

## Valves

### Admin

| Valve | Description |
|---|---|
| comfyui_image_base_url | Override the image link base URL. |

### User

| Valve | Description |
|---|---|
| comfyui_image_base_url | Overrides admin valve and COMFYUI_BASE_URL. |

## Usage

The LLM calls this tool when the user explicitly asks to enhance or upscale an image. Pass a filename from a previous generation or a direct URL to an external image.

## How it renders

The tool returns an `HTMLResponse` with `Content-Disposition: inline` plus a context tuple, so Open WebUI renders it as a **Rich UI embed**: a self-contained image viewer in a sandboxed iframe right in the chat (same viewer as Smart Generate Image).

- The image is centered, fits the chat container width, and its height is capped at **70% of the available screen height**; the viewer sizes itself after the image loads (the output dimensions are not known in advance, so there is no aspect reservation).
- Clicking the image opens a **lightbox** that fills the browser window via the **Fullscreen API** (image fit to screen, no scroll), with an X to close (top-left) and a **download** button (top-right) that forces the download.
- On close, the chat scroll position is preserved (no jump to the top).

The **LLM only receives the context** `{ "image": <url> }` (the enhanced image URL) — never the HTML. The URL is the actionable value for chained tool calls.

## Models

The workflow uses two models that are downloaded automatically on first run:

- DiT model: seedvr2_ema_7b-Q4_K_M.gguf (GGUF quantized, ~4 GB)
- VAE model: ema_vae_fp16.safetensors

## Requirements

- ComfyUI-LoadImageURL custom node installed in ComfyUI's custom_nodes/ directory.

## Workflow file

Place `enhance_image.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/enhance_image/enhance_image.json
```

The workflow JSON can be edited freely. You can replace the SeedVR2 model or configuration with any compatible upscaling setup. The tool injects parameters from both the LLM call argument (image) and the valves. Everything else uses whatever the workflow defines.

# Upscale Image

Upscales a previously generated image using SeedVR2. Loads images via URL or temporary file, auto-detecting the source type. Requires the ComfyUI-LoadImageURL custom node.

## Valves

### Admin

| Valve | Description |
|---|---|
| comfyui_image_base_url | Override the image link base URL. |

### User

| Valve | Description |
|---|---|
| seed | -1 = random, >=0 = fixed seed. |
| comfyui_image_base_url | Overrides admin valve and COMFYUI_BASE_URL. |

## Usage

The LLM calls this tool when the user explicitly asks to upscale an image. Pass a filename from a previous generation or a direct URL to an external image.

## How it renders

The tool returns an `HTMLResponse` with `Content-Disposition: inline` plus a context tuple, so Open WebUI renders it as a **Rich UI embed**: a self-contained **before/after comparison slider** in a sandboxed iframe right in the chat (the same embed as Compare Images).

- The embed shows the **original image vs the upscaled one** with an interactive divider (drag / tap / desktop hover), so the upscale quality can be inspected side by side. Both images share the same aspect ratio (SeedVR2 preserves it), so the slider fits both with `object-fit: cover`.
- The slider fills the chat container width; on portrait devices it is full width with no height cap, on landscape devices the height is capped at **80% of the available screen height** and the slider is centered.
- A floating **maximize button** (bottom-right) opens the comparison in a **fullscreen overlay with its own interactive slider** (same drag/tap/hover behavior) via the **Fullscreen API**. Escape, the restore button, or clicking the dark backdrop close it.
- On open/close, the chat scroll position is preserved (no jump to the top).

The **LLM only receives the context** `{ "image": <url> }` (the upscaled image URL) — never the HTML. The URL is the actionable value for chained tool calls.

The **LLM only receives the context** `{ "image": <url> }` (the upscaled image URL) — never the HTML. The URL is the actionable value for chained tool calls.

## Models

The workflow uses two models that are downloaded automatically on first run:

- DiT model: seedvr2_ema_7b-Q4_K_M.gguf (GGUF quantized, ~4 GB)
- VAE model: ema_vae_fp16.safetensors

## Requirements

- ComfyUI-LoadImageURL custom node installed in ComfyUI's custom_nodes/ directory.

## Workflow file

Place `seedvr2_upscale.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/upscale_image/seedvr2_upscale.json
```

The workflow JSON can be edited freely. You can replace the SeedVR2 model or configuration with any compatible upscaling setup. The tool injects parameters from both the LLM call argument (image) and the valves. Everything else uses whatever the workflow defines.

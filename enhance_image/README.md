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

## Requirements

- ComfyUI-LoadImageURL custom node installed in ComfyUI's custom_nodes/ directory.

## Workflow file

Place `enhance_image.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/enhance_image/enhance_image.json
```

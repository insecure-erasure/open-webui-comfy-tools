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

## Workflow file

Place `edit_image.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/edit_image/edit_image.json
```

The workflow JSON can be edited freely. You can replace the default Flux 2 model with any compatible checkpoint. The tool injects only the parameters configured through valves; everything else uses whatever the workflow defines.

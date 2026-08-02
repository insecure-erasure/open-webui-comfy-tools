# Virtual Try-On

Dresses a person photo with a specific upper garment and lower garment using the Flux.2 Klein try-on LoRA. Accepts three input images (person, top, bottom) via URL or temporary file, auto-detecting the source type. Requires the ComfyUI-LoadImageURL custom node.

## Valves

### Admin

| Valve | Description |
|---|---|
| comfyui_image_base_url | Override the image link base URL. |

### User

| Valve | Description |
|---|---|
| comfyui_image_base_url | Overrides admin valve and COMFYUI_BASE_URL. |
| seed | -1 = random (default), >=1 = fixed seed for reproducible results. |
| lora_config | JSON array of extra LoRAs to stack on top of the try-on LoRA. String = only name (strength 1.0), object = {"name"\|"model", "strength"}. The workflow try-on LoRA always stays in slot 1 at strength 1; your LoRAs are appended after it. Empty name, strength 0, or a name matching the try-on LoRA are skipped. Ex: `["lora1.sft", {"name": "lora2.sft", "strength": 0.5}]` |

## Usage

The LLM calls this tool when the user explicitly asks to try on clothes on a person (virtual try-on). Pass three images:

- `model_image` — photo of the person to dress
- `upper_image` — the upper garment (top, jacket, shirt...)
- `lower_image` — the lower garment (trousers, skirt, shorts...)

Each accepts a filename from a previous generation or a direct URL to an external image.

## Outputs

The tool returns:

1. **The try-on result image** — rendered from the "Random Preview Image" node (the final preview node of the workflow).
2. **The generated prompt** — extracted from the "Prompt preview" node (ShowText). The workflow builds this prompt dynamically: Florence-2 captions the subject, then the caption is combined with the garment references ("TRYON A woman. Replace the outfit with...").

## Models

The workflow downloads the following models automatically on first run:

- Diffusion model: `flux-2-klein-9b-nvfp4.safetensors`
- LoRA: `flux2\flux-klein-tryon-comfy.safetensors` (try-on LoRA, strength 1.0)
- VAE: `flux2-vae.safetensors`
- CLIP: `qwen_3_8b_nvfp4.safetensors`
- Florence-2: `Florence-2-base-ft` (subject captioning)

Generation settings: CFG 1.2, 6 steps, euler sampler, Flux2Scheduler. The latent size is derived from the model photo (`GetImageSize` → `EmptyFlux2LatentImage`), and the garments are injected via `ReferenceLatent` conditioning.

## Requirements

- ComfyUI-LoadImageURL custom node installed in ComfyUI's custom_nodes/ directory.
- rgthree-comfy (Power Lora Loader) — loads the try-on LoRA.
- ComfyUI-Custom-Scripts (ShowText node) — required to emit the generated prompt.
- ComfyUI-KJNodes (Random Preview Image node).
- ComfyUI-Florence2 (Florence-2 subject captioning).

## Workflow file

Place `virtual_try_on.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/virtual_try_on/virtual_try_on.json
```

The workflow JSON can be edited freely. The tool injects the three input images, the seed, and reads the output image + prompt from the "Random Preview Image" and "Prompt preview" nodes. Everything else uses whatever the workflow defines.

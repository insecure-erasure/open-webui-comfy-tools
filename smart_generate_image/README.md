# Smart Generate Image

Generates images from a text prompt through ComfyUI. Supports three model families with individual defaults for model checkpoint, VAE, scheduler, sampler, and CFG scale. Resolution is computed from an aspect ratio and a megapixel target using the FluxResolutionCalc node in the workflow.

## Model families

| Family | Default model | VAE | CFG | Steps | Sampler | Scheduler |
|---|---|---|---|---|---|---|
| Z-Image Turbo | zImageTurbo-mxfp8.safetensors | Z-Image_half_natural_vae.safetensors | 1.0 | 10 | euler | simple |
| Krea 2 | krea2_turbo_mixed_nvfp4.safetensors | qwen_image_vae.safetensors | 1.0 | 8 | euler | simple |
| FLUX.2 Klein | flux-2-klein-9b-nvfp4.safetensors | flux2-vae-small-bf16.safetensors | 1.0 | 8 | euler | (flux.2 guidance) |

## Valves

### Admin

| Valve | Description |
|---|---|
| model_family | Default model family. Users can override it. |
| model_name | Specific .safetensors file. Overrides the model family default. |
| default_aspect_ratio | Fallback when the LLM does not specify one. Format W:H. |
| megapixel | Target resolution independent of aspect ratio. |
| max_steps | Steps policy. 0 = user decides, -1 = force model default, 1-15 = clamp user steps. |
| lora_config | JSON array of LoRAs applied positionally. |
| comfyui_image_base_url | Override the image link base URL. |

### User

| Valve | Description |
|---|---|
| model_family | Overrides the admin valve. |
| model_name | Overrides the admin valve or workflow default. |
| steps | Inference steps. 0 = use model family default. |
| seed | -1 = random, >=0 = fixed seed. |
| lora_config | JSON array. Merged with admin LoRAs; user wins on name collision. |
| override_system_loras | When enabled, user LoRAs replace admin LoRAs entirely. |
| comfyui_image_base_url | Overrides admin valve and COMFYUI_BASE_URL. |

## Usage

The LLM calls this tool automatically when the user asks to generate an image. The prompt is written in English with enriched visual details. The aspect_ratio parameter should only be passed when the user specifically requests dimensions.

## Workflow file

Place `smart_generate_image.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/smart_generate_image/smart_generate_image.json
```

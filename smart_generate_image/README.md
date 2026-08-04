# Smart Generate Image

Generates images from a text prompt through ComfyUI. Supports three model families with individual defaults for model checkpoint, VAE, scheduler, sampler, and CFG scale. Resolution is computed from an aspect ratio and a megapixel target using the FluxResolutionCalc node in the workflow.

## Model families

| Family | Default model | VAE | CFG | Steps | Sampler | Scheduler |
|---|---|---|---|---|---|---|
| Z-Image Turbo | zImageTurbo-mxfp8.safetensors | Z-Image_half_natural_vae.safetensors | 1.0 | 10 | euler | simple |
| Krea 2 | krea2_turbo_mixed_nvfp4.safetensors | qwen_image_vae.safetensors | 1.0 | 8 | euler | simple |
| FLUX.2 Klein | flux-2-klein-9b-nvfp4.safetensors | flux2-vae-small-bf16.safetensors | 1.0 | 8 | euler | (flux.2 guidance) |

## Model sources

The models listed below use mixed quantization formats (NVFP4, MXFP8) and are designed for NVIDIA Blackwell GPUs (RTX 50xx series). They require CUDA 13 and comfy-kitchen (or a compatible runtime) to load and run correctly.

Most models and text encoders are available on HuggingFace at [InsecureErasure](https://huggingface.co/InsecureErasure):

| File | Source |
|---|---|
| zImageTurbo-mxfp8.safetensors, Z-Image_half_natural_vae.safetensors | [InsecureErasure/Z-Image-Turbo-MXFP8](https://huggingface.co/InsecureErasure/Z-Image-Turbo-MXFP8) |
| qwen3_4b_instruct_2507_mxfp8.safetensors | [InsecureErasure/Qwen3-4B-Instruct-NVFP4](https://huggingface.co/InsecureErasure/Qwen3-4B-Instruct-NVFP4) |
| qwen3_vl_4b_instruct_mxfp8.safetensors | [InsecureErasure/Qwen3-VL-4B-Instruct-NVFP4](https://huggingface.co/InsecureErasure/Qwen3-VL-4B-Instruct-NVFP4) |
| krea2_turbo_mixed_nvfp4.safetensors, qwen_image_vae.safetensors | [InsecureErasure/Krea2-Turbo-mixed-NVFP4](https://huggingface.co/InsecureErasure/Krea2-Turbo-mixed-NVFP4) |

Models for FLUX.2 Klein (flux-2-klein-9b-nvfp4, flux2-vae-small-bf16, qwen_3_8b_nvfp4) are available from community sources on HuggingFace.

## How it renders

The tool returns an `HTMLResponse` with `Content-Disposition: inline` plus a context tuple, so Open WebUI renders it as a **Rich UI embed**: a self-contained image viewer in a sandboxed iframe right in the chat (see the official [Rich UI Embedding](https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/) docs).

- The image is centered, fits the chat container width, and its height is capped at **70% of the available screen height** (approximation of 70vh of the real browser viewport — the iframe's own `vh` would refer to the small embed box). The aspect ratio (from the requested resolution) is reserved to avoid the load "jump".
- Clicking the image opens a **lightbox** that fills the browser window via the **Fullscreen API** (image fit to screen, no scroll), with an X to close (top-left) and a **download** button (top-right) that forces the download. Browsers that don't allow fullscreen (e.g. some mobile/iOS) fall back to showing it inside the embed area. On close (X / Escape / backdrop) the parent chat scroll position is restored so the page doesn't jump to the top.
- Theme follows `prefers-color-scheme`.

The **LLM only receives the context** `{ "image": <url> }` (the image URL) — never the HTML. The URL is the actionable value for chained tool calls (edit/enhance/virtual try-on/video).

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

The workflow JSON can be edited freely. You can replace the default models with any compatible checkpoint, VAE, or text encoder. Model file names, sampler settings, scheduler configuration, and resolution parameters are all determined by what the workflow nodes reference. The tool injects parameters from both the LLM call arguments (prompt, aspect_ratio) and the valves. Everything else uses whatever the workflow defines.

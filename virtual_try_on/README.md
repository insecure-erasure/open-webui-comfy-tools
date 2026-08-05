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
| prompt_suffix | Optional text appended to the end of the generated prompt, after the workflow's default try-on instruction. Leave empty to skip. |

Both garments are **optional**. When the user omits one, the tool falls back to the fixed default images `default_upper.png` / `default_lower.png` in `static/images/vton/` and shows an info notification.

Default garment images live in `static/images/vton/` (e.g. `/app/backend/open_webui/static/images/vton/default_upper.png`). The tool builds their URL from the Open WebUI base URL — the global `webui.url` config (Admin > Settings), falling back to the request's base URL — so ComfyUI can fetch them at `<open-webui-base>/static/images/vton/<filename>`. Note that `/app/backend/open_webui/static` is not inside the `data/` volume; mount a bind volume there (e.g. `-v ./static-images:/app/backend/open_webui/static/images`) so the images survive container recreation.

## Usage

The LLM calls this tool when the user explicitly asks to try on clothes on a person (virtual try-on). Pass three images:

- `model_image` — photo of the person to dress
- `upper_image` — the upper garment (top, jacket, shirt...)
- `lower_image` — the lower garment (trousers, skirt, shorts...)

Each accepts a filename from a previous generation or a direct URL to an external image.

## Outputs

The tool returns:

1. **The try-on result image** — rendered from the "Random Preview Image" node (the final preview node of the workflow), displayed as a **Rich UI embed**: a **before/after comparison slider** (the same embed as Compare Images) showing the **original model photo vs the try-on result** with an interactive divider. A floating **maximize button** (bottom-right) opens a **fullscreen overlay with its own interactive slider** — showing only the comparison, plus the **generated prompt as a caption** (over a bottom gradient) and the exit button. Escape, the restore button, or clicking the dark backdrop close it; the chat scroll is preserved.
   Two **download buttons** (top-right) force a download of the result image — one on the embed (vertically above the fullscreen button) and one in the fullscreen overlay.
   The result carries the image-viewer gallery markers, so it appears in the **conversation gallery** of the other image tools (smart_generate_image) with its generated prompt — the try-on slider itself does not navigate the gallery.
2. **The generated prompt** — extracted from the "Prompt preview" node (ShowText). The workflow builds this prompt dynamically: Florence-2 captions the subject, then the caption is combined with the garment references ("TRYON A woman. Replace the outfit with...").

In the tool result, the **LLM receives the context** `{ "image": <url>, "prompt": <text> }` (the image URL + the generated prompt) — never the HTML. The URL is actionable for chained tool calls; the prompt is used by the agent to reply to the user.

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

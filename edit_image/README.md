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
| seed | -1 = random, >=0 = fixed seed. |
| lora_config | JSON array. Merged with admin LoRAs; user wins on name collision. |
| override_system_loras | When enabled, user LoRAs replace admin LoRAs entirely. |
| comfyui_image_base_url | Overrides admin valve and COMFYUI_BASE_URL. |

## Usage

The LLM calls this tool when the user explicitly asks to edit or modify an existing image. The prompt describes the desired change in natural language (e.g., "change the background to a beach at sunset"). A random seed is generated for each edit.

## Restore mode

The `mode` argument (default `"edit"`) switches the tool to restoration:

- `mode="edit"` (default): normal edits, exactly as described above.
- `mode="restore"`: appends the `Flux2-Klein-Image-RestoreV1.safetensors` LoRA at strength 1.0 (after any admin/user LoRAs) and uses a restoration prompt. Use it for degraded images (compression artifacts, haze, soft edges, lack of detail).

The restore LoRA is validated against the server's `/models/loras` like any other LoRA, so it must be installed on the ComfyUI server. In restore mode `prompt` is **optional**: omit it (or pass an empty string `""`) and the restoration prompt is used on its own; a non-empty `prompt` is appended after the restoration prompt for extra guidance (e.g. "Restore this image to full quality").

## How it renders

The tool returns an `HTMLResponse` with `Content-Disposition: inline` plus a context tuple, so Open WebUI renders it as a **Rich UI embed**: a self-contained **before/after comparison slider** in a sandboxed iframe right in the chat (the same embed as Compare Images).

- The embed shows the **original image vs the edited one** with an interactive divider (drag / tap / desktop hover), so the edit can be inspected side by side. Both images share the same aspect ratio (the edit workflow keeps the input size), so the slider fits both with `object-fit: cover`.
- The slider fills the chat container width; on portrait devices it is full width with no height cap, on landscape devices the height is capped at **80% of the available screen height** and the slider is centered.
- A floating **maximize button** (bottom-right) opens the comparison in a **fullscreen overlay with its own interactive slider** (same drag/tap/hover behavior) via the **Fullscreen API**. The fullscreen shows only the comparison (plus the prompt caption and the exit button); Escape, the restore button, or clicking the dark backdrop close it.
- The **prompt caption** (the edit prompt) is shown at the bottom of the fullscreen over a gradient, exactly like the image viewer's lightbox.
- The edited image still appears in the **conversation gallery** of the other image tools (smart_generate_image, virtual_try_on) with its edit prompt — it uses the same gallery markers as the viewer. The edit slider itself does not navigate the gallery.
- On open/close, the chat scroll position is preserved (no jump to the top).

The **LLM only receives the context** `{ "image": <url> }` (the edited image URL) — never the HTML. The URL is the actionable value for chained tool calls.

The **LLM only receives the context** `{ "image": <url> }` (the edited image URL) — never the HTML. The URL is the actionable value for chained tool calls.

## Workflow file

Place `edit_image.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/edit_image/edit_image.json
```

The workflow JSON can be edited freely. You can replace the default Flux 2 model with any compatible checkpoint. The tool injects parameters from both the LLM call arguments (image, prompt) and the valves. Everything else uses whatever the workflow defines.

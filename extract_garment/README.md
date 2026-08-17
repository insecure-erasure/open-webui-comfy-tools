# Extract Garment

Isolates a garment from a photo: removes the background (BiRefNet), segments the requested garment (Florence-2 phrase grounding + SAM2), refines the mask, and crops the image to the garment with a solid background fill. Accepts an input image via URL or temporary file, auto-detecting the source type. Requires the ComfyUI-LoadImageURL custom node.

## Valves

### Admin

| Valve | Description |
|---|---|
| comfyui_image_base_url | Override the image link base URL. |
| background_color | Background color used in the background-removal phase to fill the area behind the garment. Default: `#c1ffff`. |

### User

| Valve | Description |
|---|---|
| comfyui_image_base_url | Overrides admin valve and COMFYUI_BASE_URL. |
| background_color | Background color for the background-removal phase. Leave empty to use the admin valve default. |

The user `background_color` valve defaults to **empty** on purpose: empty means "inherit from the admin valve". If the admin changes the color, users on the Default setting inherit it automatically.

## Usage

The LLM calls this tool when the user wants to isolate a garment from an image (e.g. to reuse it in a virtual try-on):

- `image` — photo containing the garment (filename from a previous generation or a direct external image URL)
- `garment` — the garment to extract. One of: `upper garment`, `lower garment`, `shirt`, `t-shirt`, `jacket`, `sweater`, `pullover`, `pants`, `skirt`, `trousers`. Default: `upper garment`. Matching is case/separator-insensitive (`"T-Shirt"` → `t-shirt`); unsupported values return an error listing the supported types so the model can correct itself.

## Outputs

The tool returns the extracted garment image, displayed as a **Rich UI embed**: the standard image viewer (70vh cap, click to open the lightbox with zoom, download, and gallery navigation). A small **source thumbnail** in the bottom-left corner of the frame shows the original image the garment was extracted from (~20% of the frame area, visual only — it never appears in the lightbox nor in the LLM context). The result carries the conversation-gallery markers, so it appears in the **conversation gallery** of the other image tools, with the garment name as caption (shown in the lightbox only).

In the tool result, the **LLM receives the context** `{ "image": <url> }` — the image URL, actionable for chained tool calls (e.g. `virtual_try_on` as `upper_image`/`lower_image`). The LLM never sees the HTML.

## Models

The workflow downloads the following models automatically on first run:

- BiRefNet: `BiRefNet-general` (background removal)
- Florence-2: `microsoft/Florence-2-base-ft` (phrase grounding of the garment)
- SAM2: `sam2_hiera_base_plus.safetensors` (garment segmentation)

## Requirements

- ComfyUI-LoadImageURL custom node installed in ComfyUI's custom_nodes/ directory.
- ComfyUI-Florence2 (Florence-2 phrase grounding).
- ComfyUI-KJNodes (ImageResizeKJv2, Random Preview Image).
- ComfyUI-Impact-Pack (ImpactMinMax).
- ComfyUI-RMBG (BiRefNet Remove Background, Mask Enhancer).
- ComfyUI-LayerStyle (LayerUtility: ImageRemoveAlpha).
- ComfyUI-SAM2 (DownloadAndLoadSAM2Model, Sam2Segmentation).
- ComfyUI-Easy-Use (easy mathFloat, intToFloat/floatToInt) and the BboxVisualize/Florence2toCoordinates helpers that ship with ComfyUI-Florence2.

## Workflow file

Place `extract_garment.json` in the tool's cache directory:

```
/app/backend/data/cache/tools/extract_garment/extract_garment.json
```

The workflow JSON can be edited freely. The tool injects the input image, the garment text (Florence-2 phrase grounding) and the background color, and reads the output image from the "Random Preview Image" node. Everything else uses whatever the workflow defines.

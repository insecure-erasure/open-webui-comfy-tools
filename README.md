# Open WebUI Comfy Tools

Open WebUI ships with a built-in image generation tool. It works, but its simplicity comes at a cost: no control over the model, no LoRA support, no seed for reproducibility, and no way to edit, caption, upscale, or generate video. For power users and admins running their own ComfyUI server, these limitations turn a capable backend into a black box.

This project breaks that black box open. Each tool is a standalone Python script that loads a real ComfyUI workflow from a JSON file, injects user parameters into the right nodes, and returns the result inline in the chat. Every parameter is exposed through Open WebUI valves at both admin and user level.

There is no wrapper library, no abstraction layer, and no handholding. Each tool talks directly to the ComfyUI REST API and gives you the same control you would have from the ComfyUI web interface.

All tools render their results as **Rich UI embeds**: the tool returns an `HTMLResponse` with `Content-Disposition: inline` (plus a context tuple for the LLM) and Open WebUI's middleware emits an `embeds` event, so the frontend displays a self-contained, sandboxed iframe inline in the chat — no HTML block ever reaches the LLM's context. See the official [Rich UI Embedding](https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/) documentation for how this works (height reporting via `postMessage`, sandbox flags, context tuples, prompt submission, `window.args` injection).

Image tools additionally share an **image gallery**: opening any image's lightbox (fullscreen) shows ‹ › buttons, a "n/N" counter (bottom-right) and arrow-key navigation to walk every image generated in the conversation by the four image tools (Smart Generate Image, Edit Image, Upscale Image, Virtual Try-On), with wrap-around. Like the chat itself, only the most recent messages stay mounted in the DOM, so older images leave the gallery until you scroll up. In fullscreen, images generated from a **prompt** also show the prompt overlaid at the bottom in white over a dark gradient (Smart Generate Image, Edit Image and Virtual Try-On; Upscale Image has no prompt input, so no overlay). See DESIGN.md §11–12.

## Tools

### Smart Generate Image

Generates images from a text prompt. Supports three model families: Z-Image Turbo, Krea 2, and FLUX.2 Klein. Each model family defines its own defaults for model file, VAE, scheduler, sampler, and CFG scale. See its README for valve documentation.

### Edit Image

Edits a previously generated image using Flux 2 inpainting. Accepts either a tool-generated filename or an external URL. See its README for valve documentation.

### Generate Caption

Generates a caption for an image using Florence-2 via ComfyUI. The LLM calls this tool automatically when it needs to interpret image content before editing or answering questions. The caption is always returned in English for accuracy; the LLM translates it when replying. See its README for valve documentation.

### Upscale Image

Upscales or enhances an image using SeedVR2. Loads images via URL using the ComfyUI-LoadImageURL custom node. See its README for valve documentation.

### Virtual Try-On

Dresses a person photo with an upper and a lower garment using the Flux.2 Klein try-on LoRA. Accepts three input images (model, top, bottom) via URL or filename and returns both the try-on result and the prompt generated internally by the workflow. See its README for valve documentation.

### Extract Garment

Isolates a garment from a photo (background removed, cropped to the garment) using BiRefNet + Florence-2 + SAM2. Accepts one input image via URL or filename and a garment type (upper garment, lower garment, shirt, t-shirt, jacket, sweater, pullover, pants, skirt, trousers). The result is an actionable image URL, ideal as input for Virtual Try-On. See its README for valve documentation.

### Generate Video

Generates videos from text or images using Wan 2.1 (single-path) or Wan 2.2 (dual-path high/low resolution). Frames follow a 4n+1 constraint imposed by the WAN temporal VAE stride. The result renders as a Rich UI embed: a self-contained video player (autoplay muted loop, height capped at 65vh) shown inline in the chat. See its README for valve documentation.

## Installation

### Requirements

- Open WebUI (recent version with native Tools support)
- A running ComfyUI server
- Native Tool Calling enabled
- Image Generation Engine set to ComfyUI (Admin Panel > Settings > Images)

Each workflow comes pre-configured with specific models. The models required for each tool are listed in its respective README.

The workflows also rely on custom nodes that are not part of core ComfyUI. Install the following repositories in your ComfyUI custom_nodes/ directory:

- https://github.com/insecure-erasure/ComfyUI-LoadImageURL (required by all tools)
- https://github.com/rgthree/rgthree-comfy (Power Lora Loader)
- https://github.com/kijai/ComfyUI-Florence2 (Florence-2 captioning)
- https://github.com/zkqiang/ComfyUI-SeedVR2 (SeedVR2 upscaling)
- https://github.com/kijai/ComfyUI-WanVideoWrapper (Wan 2.1 / 2.2 video generation)
- https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite (video combine)
- https://github.com/Fannovel16/ComfyUI-Frame-Interpolation (frame interpolation)
- https://github.com/kijai/ComfyUI-KJNodes (PathchSageAttention)
- https://github.com/chrisgoringe/ComfyUI-Custom-Scripts (ShowText node)
- https://github.com/ltdrdata/ComfyUI-Impact-Pack (ImpactSwitch)
- https://github.com/NVIDIA/ComfyUI-RTX-Video-SR (RTX video super resolution)

### 1. Add the tools in Open WebUI

Navigate to Workspace > Tools, click "+", paste the script content, and save.

| Script | Suggested name |
|---|---|
| smart_generate_image/tool.py | Smart Generate Image |
| extract_garment/tool.py | Extract Garment |
| upscale_image/tool.py | Upscale Image |
| virtual_try_on/tool.py | Virtual Try-On |
| edit_image/tool.py | Edit Image |
| generate_caption/tool.py | Generate Caption |
| generate_video/tool.py | Generate Video |

### 2. Deploy the workflow JSONs

Each tool requires its corresponding workflow JSON file. Copy it from the tool's directory to the tool's cache directory inside the Open WebUI container:

```
cp smart_generate_image/smart_generate_image.json /app/backend/data/cache/tools/smart_generate_image/smart_generate_image.json
cp edit_image/edit_image.json                     /app/backend/data/cache/tools/edit_image/edit_image.json
cp upscale_image/seedvr2_upscale.json            /app/backend/data/cache/tools/upscale_image/seedvr2_upscale.json
cp virtual_try_on/virtual_try_on.json             /app/backend/data/cache/tools/virtual_try_on/virtual_try_on.json
cp generate_caption/generate_caption.json         /app/backend/data/cache/tools/generate_caption/generate_caption.json
cp generate_video/generate_video.json             /app/backend/data/cache/tools/generate_video/generate_video.json
cp generate_video/generate_video_wan22.json       /app/backend/data/cache/tools/generate_video/generate_video_wan22.json
cp generate_video/generate_video.html             /app/backend/data/cache/tools/generate_video/generate_video.html
cp compare_images/compare_images.html           /app/backend/data/cache/tools/compare_images/compare_images.html
cp smart_generate_image/smart_generate_image.html /app/backend/data/cache/tools/smart_generate_image/smart_generate_image.html
cp edit_image/edit_image.html                   /app/backend/data/cache/tools/edit_image/edit_image.html
cp upscale_image/upscale_image.html             /app/backend/data/cache/tools/upscale_image/upscale_image.html
cp virtual_try_on/virtual_try_on.html           /app/backend/data/cache/tools/virtual_try_on/virtual_try_on.html
cp extract_garment/extract_garment.json          /app/backend/data/cache/tools/extract_garment/extract_garment.json
cp extract_garment/extract_garment.html          /app/backend/data/cache/tools/extract_garment/extract_garment.html
```

Each tool that renders a Rich UI embed also needs its embed template (an HTML file with the same name as the tool) copied the same way, as shown above.

The cache/tools/<name>/ directory is created automatically when you save the tool script.

### 3. Enable the tools

In any chat, open the tool selector and enable the ones you want to use.


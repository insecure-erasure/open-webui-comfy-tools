# Smart Generate Image

Open WebUI tool for AI image generation through ComfyUI -- with size control, configurable model, steps and seed via Valves.

> **Compatible with:** Open WebUI + ComfyUI

---

## Overview

Smart Generate Image is a tool that sends image generation requests to ComfyUI. The image appears in the chat rendered as markdown by the LLM. No local storage, no event emitter for files, no database persistence.

It is installed as a regular user tool in **Workspace -> Tools**. It does not require Image Generation to be enabled as a model capability or builtin tool. It uses the image generation settings from **Admin Panel -> Settings -> Images** when the engine is set to ComfyUI.

Companion tools:
- **Enhance Image** (`enhance_image.py`) — upscale previously generated images using SeedVR2.
- **Generate Video** (`generate_video.py`) — generate videos through ComfyUI (text-to-video or image-to-video).

---

## Tools

### Smart Generate Image (`smart_generate_image.py`)

Generates images through ComfyUI with control over prompt and size. Model, steps, and seed are configured via Valves.

**Parameters exposed to the LLM:**

| Parameter | Type | Description |
|-----------|------|-------------|
| prompt | string | Image description. The LLM translates to English and enriches with visual details. |
| size | string (optional) | Dimensions as WxH (e.g. 2000x3000). Falls back to Admin UI config if omitted. |

**Response format:**

```
image_md: ![Generated image](<url>)
image_filename: <filename.png>

Use image_md to display the image in your response.
```

- `image_md`: Markdown to render the image in the conversation.
- `image_filename`: Internal ComfyUI filename (not directly accessible from the filesystem). Used by Enhance Image.

### Enhance Image (`enhance_image.py`)

Upscales or enhances a previously generated image using SeedVR2. Only use when the user explicitly asks to improve, upscale, or enhance an image.

**Parameters exposed to the LLM:**

| Parameter | Type | Description |
|-----------|------|-------------|
| image_filename | string | The `image_filename` from the last Smart Generate Image response. Pass it as-is. |

**Response format:**

```
image_md: ![Enhanced image](<url>)
image_filename: <filename.png>

Use image_md to display the enhanced image in your response.
```

---

### Generate Video (`generate_video.py`)

Generates a video through ComfyUI (text-to-video or image-to-video). Uses the same `comfyui_image_base_url` valve pattern as the other tools.

**Parameters exposed to the LLM:**

| Parameter | Type | Description |
|-----------|------|-------------|
| prompt | string | Video description in English, enriched with visual motion details. |
| image_filename | string (optional) | The `image_filename` from a previous generation. Pass as-is to animate that image. |

**Response format:**

```
video_html: <full HTML block with video player>
video_filename: <filename.mp4>

Paste the video_html value inside a code block in your response
(triple backticks) so the frontend renders it as a video player.
```

The agent renders the video using an inline HTML block (`<video>` tag) instead of markdown, since markdown cannot display video.

---

## Valves

### Admin Valves (Smart Generate Image)

Configurable by admins in **Workspace -> Tools -> Smart Generate Image -> Valves**.

| Valve | Type | Default | Description |
|-------|------|---------|-------------|
| model_name | string | "" | Model/checkpoint name. Overrides the Admin UI default. Leave empty to use the Admin UI setting. |
| steps | dropdown | 0 (System default) | Inference steps (1-15, descending). 0 = inherit from Admin UI or use workflow default. |
| comfyui_image_base_url | string | "" | Public base URL for image links. Overrides COMFYUI_BASE_URL. Leave empty to use COMFYUI_BASE_URL. |

### User Valves (Smart Generate Image)

Configurable by end users from the chat interface.

| Valve | Type | Default | Description |
|-------|------|---------|-------------|
| model_name | string | "" | Your preferred model/checkpoint. Overrides the admin valve and the Admin UI setting. |
| steps | dropdown | 0 (System default) | Inference steps (1-15, descending). 0 = inherit from admin valve or Admin UI setting. |
| comfyui_image_base_url | string | "" | Override the admin valve or COMFYUI_BASE_URL for image links. |
| seed | int | -1 | Seed. -1 = random, >=0 = fixed seed for reproducibility. |

### Valves (Enhance Image)

| Valve (admin / user) | Type | Default | Description |
|----------------------|------|---------|-------------|
| comfyui_image_base_url | string | "" | Public base URL for enhanced image links. Leave empty to use COMFYUI_BASE_URL. |

### Valves (Generate Video)

Same valves as Enhance Image — only `comfyui_image_base_url` (admin / user).

### Precedence

**Model resolution:** UserValves > AdminValves > Admin UI (get_image_model) > None

**Steps resolution:** UserValves > AdminValves > Admin UI (IMAGE_STEPS) > workflow default

**Seed resolution:** UserValve. -1 = random (auto-generated), >=0 = fixed.

**Image base URL resolution:** UserValves > AdminValves > COMFYUI_BASE_URL

Both UserValves and AdminValves for steps are clamped against the Admin UI IMAGE_STEPS ceiling (or 15 as safety fallback). When clamping occurs, a warning toast is shown to the user.

---

## Configuration

Configure these in **Admin Panel -> Settings -> Images**:

- Image Generation Engine: ComfyUI
- ComfyUI Base URL: your ComfyUI server address
- ComfyUI Workflow: exported workflow JSON
- ComfyUI Workflow Nodes: define which nodes receive each parameter
- Image Size: default dimensions
- Image Steps: default inference steps (acts as ceiling for Valves)

For seed support, configure a "seed" node in Workflow Nodes.

### Workflows

Pre-configured workflows are available in the `workflows/` directory:

| File | Description |
|------|-------------|
| `zit.json` | Base workflow (zImageTurbo, no upscaler) |
| `zit-seedvr2.json` | Workflow with SeedVR2 upscaler integrated |
| `seedvr2-upscale.json` | Standalone SeedVR2 upscale workflow (used by Enhance Image) |

---

## Installation

1. Go to **Workspace -> Tools** in Open WebUI.
2. Click **"+"** and paste the contents of `smart_generate_image.py`.
3. Save as **"Smart Generate Image"**.
4. Repeat for `enhance_image.py`, save as **"Enhance Image"**.
5. Repeat for `generate_video.py`, save as **"Generate Video"**.
6. Enable the tools in the chat tool selector.

---

## Requirements

- Open WebUI (any recent version with Tools support)
- ComfyUI server running and accessible
- Native Tool Calling enabled
- ComfyUI workflow configured in Admin Panel -> Settings -> Images
- Image Generation Engine set to ComfyUI
- [ComfyUI-LoadImageURL](https://github.com/insecure-erasure/ComfyUI-LoadImageURL) custom node (required by Enhance Image)

---

## Usage

Generate an image:

> *"Generate an image of a cat wearing a spacesuit on Mars"*
>
> *"Create a 1920x1080 landscape of a cyberpunk city at night"*

Enhance a generated image:

> *"Enhance that image"*
>
> *"Upscale the last image"*

Optional details the AI can handle:

- **Size**: "... at 2000x3000"

Model, steps, and seed are controlled via Valves and Admin UI settings, not from the chat prompt.

---

## FAQ

**Q: The image doesn't show up in the chat.**  
A: Check that the ComfyUI Base URL (or comfyui_image_base_url valve) is accessible from your browser. Images are served directly from ComfyUI.

**Q: Seed doesn't seem to do anything.**  
A: Configure a "seed" node in Admin Panel -> Settings -> Images -> ComfyUI Workflow Nodes.

**Q: How do I change the model or steps?**  
A: Admins configure them in Workspace -> Tools -> Smart Generate Image -> Valves. Users can override from the chat interface. Alternatively, set defaults in Admin Panel -> Settings -> Images.

**Q: Enhance Image fails with an image loading error.**  
A: Ensure the [ComfyUI-LoadImageURL](https://github.com/insecure-erasure/ComfyUI-LoadImageURL) custom node is installed in ComfyUI's `custom_nodes/` directory.
